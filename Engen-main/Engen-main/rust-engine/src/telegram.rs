//! Optional Telegram paper-bot adapter. All portfolio state is virtual.
//! No wallet keys, signing, transaction construction, or broadcasting.

use std::{collections::HashSet, env, thread, time::Duration};
use reqwest::blocking::Client;
use serde_json::{json, Value};

const API_ROOT: &str = "https://api.telegram.org";

pub struct TelegramPaperBot {
    token: String,
    allowed_chat_ids: HashSet<String>,
    client: Client,
    offset: Option<i64>,
}

impl TelegramPaperBot {
    pub fn from_env() -> Result<Self, String> {
        let token = env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
        let allowed = env::var("TELEGRAM_ALLOWED_CHAT_IDS")
            .unwrap_or_default().split(',').map(str::trim)
            .filter(|s| !s.is_empty()).map(str::to_string).collect();
        Self::new(token, allowed)
    }

    pub fn new(token: String, allowed_chat_ids: Vec<String>) -> Result<Self, String> {
        let token = token.trim().to_string();
        if token.is_empty() { return Err("TELEGRAM_BOT_TOKEN is not configured.".into()); }
        let allowed_chat_ids = allowed_chat_ids.into_iter().map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()).collect::<HashSet<_>>();
        if allowed_chat_ids.is_empty() {
            return Err("TELEGRAM_ALLOWED_CHAT_IDS must contain an allowed chat ID.".into());
        }
        let client = Client::builder().timeout(Duration::from_secs(30)).build()
            .map_err(|_| "Telegram HTTP client initialization failed.".to_string())?;
        Ok(Self { token, allowed_chat_ids, client, offset: None })
    }

    fn api_request(&self, method: &str, payload: Option<&Value>, query: &[(&str, String)]) -> Result<Value, String> {
        let url = format!("{API_ROOT}/bot{}/{method}", self.token);
        let mut request = match payload {
            Some(value) => self.client.post(url).json(value),
            None => self.client.get(url),
        };
        if !query.is_empty() { request = request.query(query); }
        let response = request.send().map_err(|_| "Telegram API request failed.".to_string())?;
        let status_ok = response.status().is_success();
        let body: Value = response.json().map_err(|_| "Telegram API returned an invalid response.".to_string())?;
        if !status_ok || body["ok"] != true {
            return Err("Telegram API returned an unsuccessful response.".to_string());
        }
        body.get("result").cloned().ok_or_else(|| "Telegram API response has no result.".to_string())
    }

    fn chat_id(value: &Value) -> Option<String> {
        match value {
            Value::String(s) if !s.trim().is_empty() => Some(s.trim().to_string()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        }
    }

    fn reply_for_update(&self, update: &Value) -> Option<(String, String)> {
        let message = update.get("message").or_else(|| update.get("edited_message"))?.as_object()?;
        let chat = message.get("chat")?.as_object()?;
        let chat_id = Self::chat_id(chat.get("id")?)?;
        if !self.allowed_chat_ids.contains(&chat_id) { return None; }
        let text = message.get("text")?.as_str()?.trim();
        let command = text.split_whitespace().next()?.split('@').next()?.to_ascii_lowercase();
        let reply = match command.as_str() {
            "/start" | "/help" => "Engen paper-trading controls (virtual only):\n/status - portfolio status\n/balance - virtual balance\n/settings - demo risk limit\n/history - recent simulated trades\n/pause - pause simulated trades\n/resume - resume simulated trades\nNo real transactions are sent.".to_string(),
            "/status" => "Mode: PAPER\nLive trading: disabled".to_string(),
            "/balance" => "Virtual ledger is local to this bot process. No real wallet balance is read.".to_string(),
            "/settings" => "Paper-only settings. Live trading is disabled.".to_string(),
            "/history" => "No simulated trades yet.".to_string(),
            "/pause" => "Paper trading paused. No real trades are possible.".to_string(),
            "/resume" => "Paper trading resumed. Virtual trades only.".to_string(),
            _ => "Unknown command. Use /help.".to_string(),
        };
        Some((chat_id, reply))
    }

    pub fn handle_update(&self, update: &Value) -> Result<(), String> {
        if let Some((chat_id, message)) = self.reply_for_update(update) {
            self.api_request("sendMessage", Some(&json!({"chat_id":chat_id,"text":message})), &[])?;
        }
        Ok(())
    }

    pub fn run_forever(&mut self) -> Result<(), String> {
        loop {
            let mut query = vec![("timeout", "20".to_string())];
            if let Some(offset) = self.offset { query.push(("offset", offset.to_string())); }
            let updates = self.api_request("getUpdates", None, &query)?;
            let updates = updates.as_array().ok_or_else(|| "Telegram returned an invalid updates list.".to_string())?;
            for update in updates {
                if let Some(id) = update.get("update_id").and_then(Value::as_i64) {
                    self.offset = Some(id.saturating_add(1));
                }
                self.handle_update(update)?;
            }
            thread::sleep(Duration::from_secs(1));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bot() -> TelegramPaperBot {
        TelegramPaperBot::new("test-token".into(), vec!["12345".into()]).unwrap()
    }

    #[test]
    fn requires_token_and_allowlist() {
        assert!(TelegramPaperBot::new("".into(), vec!["1".into()]).is_err());
        assert!(TelegramPaperBot::new("token".into(), vec![]).is_err());
    }

    #[test]
    fn ignores_unauthorized_chats_and_supports_virtual_commands() {
        let bot = bot();
        let unauthorized = json!({"message":{"chat":{"id":999},"text":"/pause"}});
        assert!(bot.reply_for_update(&unauthorized).is_none());
        let allowed = json!({"edited_message":{"chat":{"id":12345},"text":"/help@test_bot"}});
        let (_, reply) = bot.reply_for_update(&allowed).unwrap();
        assert!(reply.contains("/balance"));
        assert!(reply.contains("No real transactions"));
    }

    #[test]
    fn only_virtual_controls_are_exposed() {
        let bot = bot();
        for command in ["/status", "/balance", "/settings", "/history", "/pause", "/resume"] {
            let payload = json!({"message":{"chat":{"id":12345},"text":command}});
            let (_, reply) = bot.reply_for_update(&payload).unwrap();
            assert!(reply.contains("disabled") || reply.contains("virtual") || reply.contains("paused") || reply.contains("No simulated"));
        }
    }
}
