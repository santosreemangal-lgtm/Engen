//! Optional Telegram adapter for a virtual paper portfolio.
//!
//! The adapter preserves the legacy paper-only status/balance/settings/history
//! and pause/resume command surface. It requires an explicit chat allowlist.
//! It has no Solana transaction signing, wallet-key handling, or broadcast API.

use std::collections::{BTreeMap, HashSet};
use std::env;
use std::thread;
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use reqwest::blocking::{Client, RequestBuilder};
use serde_json::{json, Value};

const API_ROOT: &str = "https://api.telegram.org";
const DEFAULT_STARTING_BALANCE: f64 = 1_000.0;
const DEFAULT_MAX_POSITION_FRACTION: f64 = 0.25;
const TELEGRAM_LONG_POLL_SECONDS: u64 = 20;

#[derive(Debug, Clone, PartialEq)]
pub struct PaperPosition {
    pub quantity: f64,
    pub average_entry_price: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PaperTrade {
    pub timestamp: String,
    pub side: String,
    pub token: String,
    pub quantity: f64,
    pub price: f64,
    pub cash_delta: f64,
    pub realized_pnl: f64,
}

#[derive(Debug, Clone)]
pub struct PaperTrader {
    initial_balance: f64,
    cash: f64,
    max_position_fraction: f64,
    positions: BTreeMap<String, PaperPosition>,
    history: Vec<PaperTrade>,
    paused: bool,
}

impl Default for PaperTrader {
    fn default() -> Self {
        Self::new(DEFAULT_STARTING_BALANCE, DEFAULT_MAX_POSITION_FRACTION)
            .expect("static default paper settings are valid")
    }
}

impl PaperTrader {
    pub fn new(starting_balance: f64, max_position_fraction: f64) -> Result<Self, String> {
        if !starting_balance.is_finite() || starting_balance <= 0.0 {
            return Err("starting_balance must be a positive finite number.".to_string());
        }
        if !max_position_fraction.is_finite()
            || max_position_fraction <= 0.0
            || max_position_fraction > 1.0
        {
            return Err("max_position_fraction must be in (0, 1].".to_string());
        }
        Ok(Self {
            initial_balance: starting_balance,
            cash: starting_balance,
            max_position_fraction,
            positions: BTreeMap::new(),
            history: Vec::new(),
            paused: false,
        })
    }

    fn validate_token(token: &str) -> Result<String, String> {
        let token = token.trim();
        if token.is_empty() {
            return Err("A token identifier is required.".to_string());
        }
        Ok(token.to_string())
    }

    fn validate_positive(value: f64, field: &str) -> Result<(), String> {
        if !value.is_finite() || value <= 0.0 {
            return Err(format!("{field} must be a positive finite number."));
        }
        Ok(())
    }

    fn round8(value: f64) -> f64 {
        (value * 100_000_000.0).round() / 100_000_000.0
    }

    pub fn status(&self) -> Value {
        let positions: serde_json::Map<String, Value> = self
            .positions
            .iter()
            .map(|(token, position)| {
                (
                    token.clone(),
                    json!({
                        "quantity": Self::round8(position.quantity),
                        "average_entry_price": Self::round8(position.average_entry_price)
                    }),
                )
            })
            .collect();

        json!({
            "mode": "paper",
            "paused": self.paused,
            "cash": Self::round8(self.cash),
            "starting_balance": Self::round8(self.initial_balance),
            "positions": positions,
            "history_count": self.history.len(),
            "live_trading_enabled": false
        })
    }

    pub fn pause(&mut self) -> Value {
        self.paused = true;
        self.status()
    }

    pub fn resume(&mut self) -> Value {
        self.paused = false;
        self.status()
    }

    pub fn buy(&mut self, token: &str, quantity: f64, price: f64) -> Result<PaperTrade, String> {
        let token = Self::validate_token(token)?;
        Self::validate_positive(quantity, "quantity")?;
        Self::validate_positive(price, "price")?;
        if self.paused {
            return Err("Paper trading is paused.".to_string());
        }

        let cost = quantity * price;
        let resulting_cash = self.cash - cost;
        if !cost.is_finite() || !resulting_cash.is_finite() {
            return Err("Trade value must be finite.".to_string());
        }
        if cost > self.cash {
            return Err("Insufficient virtual cash.".to_string());
        }

        let existing = self.positions.get(&token);
        let old_quantity = existing.map(|position| position.quantity).unwrap_or(0.0);
        let old_cost = existing
            .map(|position| position.quantity * position.average_entry_price)
            .unwrap_or(0.0);
        let max_cost = self.initial_balance * self.max_position_fraction;
        if old_cost + cost > max_cost + 1e-9 {
            return Err("Position size exceeds the configured risk limit.".to_string());
        }

        let new_quantity = old_quantity + quantity;
        let new_average = (old_cost + cost) / new_quantity;
        if !new_quantity.is_finite() || !new_average.is_finite() {
            return Err("Resulting position must be finite.".to_string());
        }

        self.positions.insert(
            token.clone(),
            PaperPosition {
                quantity: new_quantity,
                average_entry_price: new_average,
            },
        );
        self.cash = resulting_cash;
        Ok(self.record("buy", token, quantity, price, -cost, 0.0))
    }

    pub fn sell(&mut self, token: &str, quantity: f64, price: f64) -> Result<PaperTrade, String> {
        let token = Self::validate_token(token)?;
        Self::validate_positive(quantity, "quantity")?;
        Self::validate_positive(price, "price")?;
        if self.paused {
            return Err("Paper trading is paused.".to_string());
        }

        let position = self
            .positions
            .get(&token)
            .cloned()
            .ok_or_else(|| "Insufficient virtual token quantity.".to_string())?;
        if quantity > position.quantity + 1e-9 {
            return Err("Insufficient virtual token quantity.".to_string());
        }

        let proceeds = quantity * price;
        let pnl = (price - position.average_entry_price) * quantity;
        let resulting_cash = self.cash + proceeds;
        if !proceeds.is_finite() || !pnl.is_finite() || !resulting_cash.is_finite() {
            return Err("Trade value and realized P&L must be finite.".to_string());
        }

        let remaining = position.quantity - quantity;
        if remaining <= 1e-9 {
            self.positions.remove(&token);
        } else if let Some(existing) = self.positions.get_mut(&token) {
            existing.quantity = remaining;
        }
        self.cash = resulting_cash;
        Ok(self.record("sell", token, quantity, price, proceeds, pnl))
    }

    fn record(
        &mut self,
        side: &str,
        token: String,
        quantity: f64,
        price: f64,
        cash_delta: f64,
        realized_pnl: f64,
    ) -> PaperTrade {
        let trade = PaperTrade {
            timestamp: Utc::now().to_rfc3339_opts(SecondsFormat::AutoSi, true),
            side: side.to_string(),
            token,
            quantity: Self::round8(quantity),
            price: Self::round8(price),
            cash_delta: Self::round8(cash_delta),
            realized_pnl: Self::round8(realized_pnl),
        };
        self.history.push(trade.clone());
        trade
    }

    pub fn history(&self, limit: usize) -> Result<Vec<PaperTrade>, String> {
        if limit == 0 {
            return Err("history limit must be a positive integer.".to_string());
        }
        let start = self.history.len().saturating_sub(limit);
        Ok(self.history[start..].to_vec())
    }
}

pub struct TelegramPaperBot {
    token: String,
    allowed_chat_ids: HashSet<String>,
    client: Client,
    offset: Option<i64>,
    trader: PaperTrader,
}

impl TelegramPaperBot {
    pub fn from_env() -> Result<Self, String> {
        let token = env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
        let allowed_chat_ids = env::var("TELEGRAM_ALLOWED_CHAT_IDS")
            .unwrap_or_default()
            .split(',')
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .collect::<Vec<_>>();
        Self::new(token, allowed_chat_ids, PaperTrader::default())
    }

    pub fn new(
        token: String,
        allowed_chat_ids: Vec<String>,
        trader: PaperTrader,
    ) -> Result<Self, String> {
        let token = token.trim().to_string();
        if token.is_empty() {
            return Err("TELEGRAM_BOT_TOKEN is not configured.".to_string());
        }
        let allowed_chat_ids = allowed_chat_ids
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .collect::<HashSet<_>>();
        if allowed_chat_ids.is_empty() {
            return Err(
                "TELEGRAM_ALLOWED_CHAT_IDS must contain at least one allowed chat ID.".to_string(),
            );
        }

        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "Telegram HTTP client initialization failed.".to_string())?;

        Ok(Self {
            token,
            allowed_chat_ids,
            client,
            offset: None,
            trader,
        })
    }

    fn request_builder(
        &self,
        method: &str,
        payload: Option<&Value>,
        query: Option<&[(String, String)]>,
    ) -> RequestBuilder {
        let url = format!("{API_ROOT}/bot{}/{method}", self.token);
        let mut request = match payload {
            Some(body) => self.client.post(url).json(body),
            None => self.client.get(url),
        };
        if let Some(query) = query {
            request = request.query(query);
        }
        request
    }

    fn request(
        &self,
        method: &str,
        payload: Option<&Value>,
        query: Option<&[(String, String)]>,
    ) -> Result<Value, String> {
        let response = self
            .request_builder(method, payload, query)
            .send()
            .map_err(|_| "Telegram API request failed.".to_string())?;
        let successful_status = response.status().is_success();
        let body: Value = response
            .json()
            .map_err(|_| "Telegram API returned an invalid response.".to_string())?;
        if !successful_status || body.get("ok") != Some(&Value::Bool(true)) {
            return Err("Telegram API returned an unsuccessful response.".to_string());
        }
        body.get("result")
            .cloned()
            .ok_or_else(|| "Telegram API response did not contain a result.".to_string())
    }

    pub fn send_message(&self, chat_id: &str, message: &str) -> Result<(), String> {
        let payload = json!({"chat_id": chat_id, "text": message});
        self.request("sendMessage", Some(&payload), None)?;
        Ok(())
    }

    fn chat_id_string(value: &Value) -> Option<String> {
        match value {
            Value::String(value) if !value.trim().is_empty() => Some(value.trim().to_string()),
            Value::Number(value) => Some(value.to_string()),
            _ => None,
        }
    }

    fn reply_for_update(&mut self, update: &Value) -> Option<(String, String)> {
        let message = update
            .get("message")
            .or_else(|| update.get("edited_message"))?
            .as_object()?;
        let chat = message.get("chat")?.as_object()?;
        let chat_id = Self::chat_id_string(chat.get("id")?)?;
        if !self.allowed_chat_ids.contains(&chat_id) {
            return None;
        }

        let text = message.get("text")?.as_str()?.trim();
        let command = text
            .split_whitespace()
            .next()?
            .split('@')
            .next()?
            .to_ascii_lowercase();

        let reply = match command.as_str() {
            "/start" | "/help" => {
                "Engen paper-trading controls (virtual only):\n\
                 /status - portfolio status\n\
                 /balance - virtual cash and positions\n\
                 /settings - current demo risk limit\n\
                 /history - recent simulated trades\n\
                 /pause - block simulated trades\n\
                 /resume - allow simulated trades\n\
                 No real transactions are sent."
                    .to_string()
            }
            "/status" => {
                let status = self.trader.status();
                let positions = status["positions"].as_object().map_or(0, |items| items.len());
                format!(
                    "Mode: PAPER\nPaused: {}\nVirtual cash: {}\nPositions: {}\nLive trading: disabled",
                    status["paused"].as_bool().unwrap_or(true),
                    status["cash"].as_f64().unwrap_or(0.0),
                    positions
                )
            }
            "/balance" => {
                let status = self.trader.status();
                let positions = status["positions"]
                    .as_object()
                    .map(|items| {
                        items
                            .iter()
                            .map(|(token, item)| {
                                format!(
                                    "{token}: {} units @ avg {}",
                                    item["quantity"].as_f64().unwrap_or(0.0),
                                    item["average_entry_price"].as_f64().unwrap_or(0.0)
                                )
                            })
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                format!(
                    "Virtual cash: {}\nPositions: {}\n{}",
                    status["cash"].as_f64().unwrap_or(0.0),
                    positions.len(),
                    if positions.is_empty() {
                        "No open positions.".to_string()
                    } else {
                        positions.join("\n")
                    }
                )
            }
            "/settings" => format!(
                "Paper-only settings:\nStarting virtual balance: {}\nMaximum position cost: {}% of starting balance\nLive trading: disabled",
                self.trader.initial_balance,
                (self.trader.max_position_fraction * 100.0) as u32
            ),
            "/history" => {
                let entries = self.trader.history(10).unwrap_or_default();
                let text = entries
                    .iter()
                    .rev()
                    .map(|item| {
                        format!(
                            "{} {} qty={} price={} realized P/L={}",
                            item.side.to_ascii_uppercase(),
                            item.token,
                            item.quantity,
                            item.price,
                            item.realized_pnl
                        )
                    })
                    .collect::<Vec<_>>();
                format!(
                    "Recent simulated trades:\n{}",
                    if text.is_empty() {
                        "No simulated trades yet.".to_string()
                    } else {
                        text.join("\n")
                    }
                )
            }
            "/pause" => {
                self.trader.pause();
                "Paper trading paused. No real trades are possible.".to_string()
            }
            "/resume" => {
                self.trader.resume();
                "Paper trading resumed. Virtual trades only.".to_string()
            }
            _ => "Unknown command. Use /help.".to_string(),
        };

        Some((chat_id, reply))
    }

    pub fn handle_update(&mut self, update: &Value) -> Result<(), String> {
        if let Some((chat_id, message)) = self.reply_for_update(update) {
            self.send_message(&chat_id, &message)?;
        }
        Ok(())
    }

    fn poll_updates(&self) -> Result<Vec<Value>, String> {
        let mut query = vec![(
            "timeout".to_string(),
            TELEGRAM_LONG_POLL_SECONDS.to_string(),
        )];
        if let Some(offset) = self.offset {
            query.push(("offset".to_string(), offset.to_string()));
        }
        let updates = self.request("getUpdates", None, Some(&query))?;
        updates
            .as_array()
            .cloned()
            .ok_or_else(|| "Telegram returned an invalid updates list.".to_string())
    }

    /// Optional long-poll process; the dashboard does not start it implicitly.
    pub fn run_forever(&mut self) -> Result<(), String> {
        loop {
            let updates = self.poll_updates()?;
            for update in updates {
                if let Some(update_id) = update.get("update_id").and_then(Value::as_i64) {
                    self.offset = Some(update_id.saturating_add(1));
                }
                self.handle_update(&update)?;
            }
            thread::sleep(Duration::from_secs(1));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn update(chat_id: i64, text: &str) -> Value {
        json!({
            "update_id": 1,
            "message": {"chat": {"id": chat_id}, "text": text}
        })
    }

    fn bot() -> TelegramPaperBot {
        TelegramPaperBot::new(
            "test-token".to_string(),
            vec!["12345".to_string()],
            PaperTrader::default(),
        )
        .unwrap()
    }

    #[test]
    fn paper_trader_buy_sell_and_realized_pnl_match_reference_behavior() {
        let mut trader = PaperTrader::new(1_000.0, 0.5).unwrap();
        let buy = trader.buy("TOKEN", 2.0, 100.0).unwrap();
        let sell = trader.sell("TOKEN", 1.0, 125.0).unwrap();
        let status = trader.status();
        assert_eq!(buy.side, "buy");
        assert_eq!(sell.realized_pnl, 25.0);
        assert_eq!(status["cash"].as_f64(), Some(925.0));
        assert_eq!(status["positions"]["TOKEN"]["quantity"].as_f64(), Some(1.0));
        assert_eq!(status["live_trading_enabled"], false);
    }

    #[test]
    fn paper_trader_rejects_risk_limit_oversell_and_paused_orders() {
        let mut trader = PaperTrader::default();
        assert!(trader.buy("TOKEN", 4.0, 100.0).is_err());
        assert!(trader.sell("TOKEN", 1.0, 10.0).is_err());
        trader.pause();
        assert!(trader.buy("TOKEN", 1.0, 10.0).is_err());
        trader.resume();
        assert!(trader.buy("TOKEN", 1.0, 10.0).is_ok());
    }

    #[test]
    fn bot_requires_token_and_explicit_chat_allowlist() {
        assert!(TelegramPaperBot::new(
            "".to_string(),
            vec!["12345".to_string()],
            PaperTrader::default()
        )
        .is_err());
        assert!(
            TelegramPaperBot::new("token".to_string(), vec![], PaperTrader::default()).is_err()
        );
    }

    #[test]
    fn unauthorized_chat_is_ignored() {
        let mut bot = bot();
        assert!(bot.reply_for_update(&update(99999, "/pause")).is_none());
        assert_eq!(bot.trader.status()["paused"], false);
    }

    #[test]
    fn help_and_status_commands_are_virtual_only() {
        let mut bot = bot();
        let (_, help) = bot.reply_for_update(&update(12345, "/help")).unwrap();
        assert!(help.contains("/balance"));
        assert!(help.contains("No real transactions"));
        let (_, status) = bot.reply_for_update(&update(12345, "/status")).unwrap();
        assert!(status.contains("Mode: PAPER"));
        assert!(status.contains("Live trading: disabled"));
    }

    #[test]
    fn balance_settings_history_pause_and_resume_commands_work() {
        let mut bot = bot();
        for command in ["/balance", "/settings", "/history"] {
            assert!(bot.reply_for_update(&update(12345, command)).is_some());
        }
        let (_, pause) = bot.reply_for_update(&update(12345, "/pause")).unwrap();
        assert!(pause.contains("paused"));
        assert_eq!(bot.trader.status()["paused"], true);
        let (_, resume) = bot.reply_for_update(&update(12345, "/resume")).unwrap();
        assert!(resume.contains("resumed"));
        assert_eq!(bot.trader.status()["paused"], false);
    }

    #[test]
    fn edited_messages_and_bot_suffixes_are_supported() {
        let mut bot = bot();
        let payload = json!({
            "edited_message": {
                "chat": {"id": 12345},
                "text": "/help@engen_bot"
            }
        });
        let (_, reply) = bot.reply_for_update(&payload).unwrap();
        assert!(reply.contains("virtual only"));
    }
}
