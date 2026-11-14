//! Tiny paper HTTP API. No keys, no signing, no broadcast.
//!
//! GET  /health
//! GET  /api/paper
//! POST /api/candidates   JSON array of MonitorCandidate
//! POST /api/fill         JSON { "mint": "...", "price_usd": "2", "liquidity_usd": "20000" }

use crate::monitor_service::MonitorService;
use crate::paper_copy::MonitorCandidate;
use crate::paper_simulator::{MarketPrice, PaperSimulator};
use crate::parser::classify_balance_changes;
use crate::pipeline::process_observation;
use crate::profile_store::ProfileStore;
use crate::signature_store::SignatureStore;
use rust_decimal::Decimal;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone)]
pub struct PaperApiState {
    pub sim: Arc<Mutex<PaperSimulator>>,
    pub monitor: Arc<Mutex<MonitorService>>,
    pub profiles: Arc<Mutex<ProfileStore>>,
    pub signatures: Arc<Mutex<SignatureStore>>,
    pub api_token: Option<String>,
    pub hits: Arc<Mutex<Vec<u64>>>,
}

impl PaperApiState {
    pub fn for_tests() -> Self {
        let path = std::env::temp_dir().join(format!("engen-http-{}.db", uuid::Uuid::new_v4()));
        let sig = std::env::temp_dir().join(format!("engen-sig-{}.db", uuid::Uuid::new_v4()));
        Self {
            sim: Arc::new(Mutex::new(PaperSimulator::new())),
            monitor: Arc::new(Mutex::new(MonitorService::new())),
            profiles: Arc::new(Mutex::new(ProfileStore::open(&path).expect("profile db"))),
            signatures: Arc::new(Mutex::new(SignatureStore::open(&sig).expect("sig db"))),
            api_token: None,
            hits: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

pub fn handle_http(request: &str, state: &PaperApiState) -> String {
    let mut lines = request.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let target = parts.next().unwrap_or("/");
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let body = request.split("\r\n\r\n").nth(1).unwrap_or("");

    if path.starts_with("/api/") && !authorized(request, state.api_token.as_deref()) {
        return respond(401, &serde_json::json!({"error": "Dashboard API authentication required."}));
    }
    if path.starts_with("/api/") && !allow_request(&state.hits) {
        return respond(429, &serde_json::json!({"error": "API request limit reached. Wait before retrying."}));
    }

    let (status, payload) = match (method, path) {
        ("GET", "/") | ("GET", "/index.html") => {
            return html_index();
        }
        ("GET", "/health") => (
            200,
            serde_json::json!({
                "ok": true,
                "mode": "PAPER_SIMULATION",
                "real_trading": false,
                "transaction_broadcast": false
            }),
        ),
        ("GET", "/api/paper") | ("GET", "/api/paper-simulation") => {
            let sim = state.sim.lock().unwrap_or_else(|e| e.into_inner());
            (200, paper_report(&sim))
        }
        ("GET", "/api/wallets") => {
            let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
            match store.wallets() {
                Ok(wallets) => (200, serde_json::json!({"wallets": wallets, "real_trading": false})),
                Err(err) => (500, serde_json::json!({"error": err.to_string()})),
            }
        }
        ("GET", "/api/profiles") => {
            let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
            match store.list() {
                Ok(profiles) => (
                    200,
                    serde_json::json!({"profiles": profiles, "real_trading": false}),
                ),
                Err(err) => (500, serde_json::json!({"error": err.to_string()})),
            }
        }
        ("GET", "/api/monitor") => {
            let monitor = state.monitor.lock().unwrap_or_else(|e| e.into_inner());
            let wallets: Vec<_> = monitor.monitors.iter().map(|m| m.wallet.clone()).collect();
            (
                200,
                serde_json::json!({
                    "enabled": !wallets.is_empty(),
                    "interval_seconds": 15,
                    "wallets": wallets,
                    "real_trading": false,
                    "transaction_broadcast": false
                }),
            )
        }
        ("GET", "/api/rpc-health") => (
            200,
            serde_json::json!({
                "mode": "READ_ONLY",
                "available": false,
                "status": "paper_only",
                "ok": true,
                "real_trading": false,
                "transaction_broadcast": false
            }),
        ),
        ("GET", "/api/profile-monitor") => {
            let id = query_param(query, "profile_id");
            let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
            match store.monitor_status(&id) {
                Ok(body) => (200, body),
                Err(err) => status_for_profile(err),
            }
        }
        ("POST", "/api/wallets") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => {
                let address = value.get("address").and_then(|v| v.as_str()).unwrap_or("");
                let action = value.get("action").and_then(|v| v.as_str()).unwrap_or("add");
                let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
                let result = if action == "remove" {
                    store.remove_wallet(address)
                } else {
                    store.add_wallet(address)
                };
                match result {
                    Ok(wallets) => (200, serde_json::json!({"wallets": wallets})),
                    Err(err) => (400, serde_json::json!({"error": err.to_string()})),
                }
            }
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        ("POST", "/api/profiles") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => profile_action(state, &value),
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        ("POST", "/api/profile-monitor") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => {
                let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
                let action = value.get("action").and_then(|v| v.as_str()).unwrap_or("");
                let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
                let enabled = match action {
                    "start" => true,
                    "stop" => false,
                    _ => {
                        return respond(400, &serde_json::json!({"error": "Action must be start or stop."}));
                    }
                };
                match store.set_monitor(id, enabled) {
                    Ok(body) => {
                        if enabled {
                            if let Ok(Some(profile)) = store.get(id) {
                                let mut monitor = state.monitor.lock().unwrap_or_else(|e| e.into_inner());
                                for wallet in profile.source_wallets {
                                    monitor.add_wallet(wallet);
                                }
                            }
                        }
                        (200, body)
                    }
                    Err(err) => status_for_profile(err),
                }
            }
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        ("POST", "/api/paper-simulation") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => paper_action(state, &value),
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        ("GET", "/api/profile-transactions") => {
            let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
            match store.list_swaps(50) {
                Ok(rows) => (
                    200,
                    serde_json::json!({
                        "transactions": rows,
                        "real_trading": false,
                        "transaction_broadcast": false
                    }),
                ),
                Err(err) => (500, serde_json::json!({"error": err.to_string()})),
            }
        }
        ("POST", "/api/classify") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => {
                let wallet = value.get("wallet").and_then(|v| v.as_str()).unwrap_or("");
                let changes = value
                    .get("token_balance_changes")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                let signal = classify_balance_changes(wallet, &changes);
                if let Some(signature) = value.get("signature").and_then(|v| v.as_str()) {
                    let class = signal.get("classification").and_then(|v| v.as_str()).unwrap_or("UNKNOWN");
                    let asset = signal["asset_mints"].get(0).and_then(|v| v.as_str());
                    let quote = signal["quote_mints"].get(0).and_then(|v| v.as_str());
                    let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
                    let _ = store.record_swap(signature, wallet, class, asset, quote);
                }
                (200, signal)
            }
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        ("POST", "/api/process") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => {
                let mut monitor = state.monitor.lock().unwrap_or_else(|e| e.into_inner());
                let mut sim = state.sim.lock().unwrap_or_else(|e| e.into_inner());
                let signatures = state.signatures.lock().unwrap_or_else(|e| e.into_inner());
                let profiles = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
                match process_observation(&mut monitor, &mut sim, &signatures, &profiles, &value) {
                    Ok(body) => (200, body),
                    Err(err) => (500, serde_json::json!({"error": err.to_string()})),
                }
            }
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        ("POST", "/api/candidates") => match serde_json::from_str::<Vec<MonitorCandidate>>(body) {
            Ok(items) => {
                let mut monitor = state.monitor.lock().unwrap_or_else(|e| e.into_inner());
                let mut sim = state.sim.lock().unwrap_or_else(|e| e.into_inner());
                let mut wallets = Vec::new();
                for item in &items {
                    if !wallets.iter().any(|w| w == &item.wallet) {
                        wallets.push(item.wallet.clone());
                        monitor.add_wallet(&item.wallet);
                    }
                }
                for wallet in &wallets {
                    let batch: Vec<_> = items
                        .iter()
                        .filter(|c| &c.wallet == wallet)
                        .cloned()
                        .collect();
                    monitor.ingest(wallet, batch);
                }
                let queued = monitor.tick(&mut sim).unwrap_or(0);
                (
                    200,
                    serde_json::json!({
                        "queued": queued,
                        "real_trading": false,
                        "transaction_broadcast": false
                    }),
                )
            }
            Err(err) => (
                400,
                serde_json::json!({"error": format!("invalid candidates: {err}")}),
            ),
        },
        ("POST", "/api/fill") => match serde_json::from_str::<serde_json::Value>(body) {
            Ok(value) => {
                let mint = value.get("mint").and_then(|v| v.as_str()).unwrap_or("");
                let price = value
                    .get("price_usd")
                    .and_then(|v| v.as_str())
                    .and_then(|s| Decimal::from_str(s).ok());
                let liq = value
                    .get("liquidity_usd")
                    .and_then(|v| v.as_str())
                    .and_then(|s| Decimal::from_str(s).ok())
                    .unwrap_or(Decimal::from(20_000));
                let Some(price_usd) = price else {
                    return respond(
                        400,
                        &serde_json::json!({"error": "price_usd string required"}),
                    );
                };
                if mint.is_empty() {
                    return respond(400, &serde_json::json!({"error": "mint required"}));
                }
                let market = MarketPrice {
                    price_usd,
                    liquidity_usd: liq,
                    age_seconds: 1,
                };
                let mut monitor = state.monitor.lock().unwrap_or_else(|e| e.into_inner());
                let mut sim = state.sim.lock().unwrap_or_else(|e| e.into_inner());
                match monitor.tick_and_fill(&mut sim, &|m| {
                    if m == mint {
                        Some(market.clone())
                    } else {
                        None
                    }
                }) {
                    Ok(outcomes) => (
                        200,
                        serde_json::json!({
                            "outcomes": outcomes,
                            "real_trading": false,
                            "transaction_broadcast": false
                        }),
                    ),
                    Err(err) => (500, serde_json::json!({"error": err.to_string()})),
                }
            }
            Err(err) => (400, serde_json::json!({"error": err.to_string()})),
        },
        _ => (404, serde_json::json!({"error": "not found"})),
    };
    respond(status, &payload)
}

fn query_param(query: &str, key: &str) -> String {
    query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .find(|(k, _)| *k == key)
        .map(|(_, v)| v.to_string())
        .unwrap_or_default()
}

fn status_for_profile(err: crate::EngenError) -> (u16, serde_json::Value) {
    let message = err.to_string();
    let code = if message.contains("Profile not found") {
        404
    } else if message.contains("Activate this profile") {
        409
    } else {
        400
    };
    (code, serde_json::json!({"error": message}))
}

fn profile_action(state: &PaperApiState, value: &serde_json::Value) -> (u16, serde_json::Value) {
    let action = value.get("action").and_then(|v| v.as_str()).unwrap_or("create");
    let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
    let result = match action {
        "create" => store
            .create_profile(value.get("name").and_then(|v| v.as_str()).unwrap_or(""))
            .map(|profile| (201, serde_json::json!({"profile": profile, "simulated": true}))),
        "update" => {
            let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
            let status = value.get("status").and_then(|v| v.as_str()).unwrap_or("draft");
            store
                .set_status(id, status)
                .map(|profile| (200, serde_json::json!({"profile": profile, "simulated": true})))
        }
        "settings" => {
            let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
            let settings = value.get("settings").cloned().unwrap_or(serde_json::json!({}));
            store
                .set_settings(id, &settings)
                .map(|profile| (200, serde_json::json!({"profile": profile, "simulated": true})))
        }
        "associate-source-wallet" => {
            let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
            let wallet = value.get("wallet_address").and_then(|v| v.as_str()).unwrap_or("");
            store
                .associate_wallet(id, wallet)
                .map(|profile| (200, serde_json::json!({"profile": profile, "simulated": true})))
        }
        "disassociate-source-wallet" => {
            let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
            let wallet = value.get("wallet_address").and_then(|v| v.as_str()).unwrap_or("");
            store
                .disassociate_wallet(id, wallet)
                .map(|profile| (200, serde_json::json!({"profile": profile, "simulated": true})))
        }
        "delete" => {
            let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
            store.delete_profile(id).map(|_| {
                (
                    200,
                    serde_json::json!({"deleted": true, "profile_id": id, "history_preserved": true}),
                )
            })
        }
        _ => Err(crate::EngenError::InvalidInput(
            "Action must be create, update, settings, associate-source-wallet, disassociate-source-wallet, or delete.".into(),
        )),
    };
    match result {
        Ok(pair) => pair,
        Err(err) => status_for_profile(err),
    }
}

fn paper_report(sim: &crate::paper_simulator::PaperSimulator) -> serde_json::Value {
    let mut report = sim.report();
    let usd = report["balances"].get("USD").cloned().unwrap_or(serde_json::json!("0"));
    report["mode"] = serde_json::json!("PAPER_SIMULATION");
    report["simulated"] = serde_json::json!(true);
    report["database_available"] = serde_json::json!(true);
    report["real_wallet_balances_included"] = serde_json::json!(false);
    report["real_trading"] = serde_json::json!(false);
    report["transaction_broadcast"] = serde_json::json!(false);
    report["available_balance"] = usd.clone();
    report["available_balance_usd"] = usd;
    report
}

fn paper_action(state: &PaperApiState, value: &serde_json::Value) -> (u16, serde_json::Value) {
    let action = value.get("action").and_then(|v| v.as_str()).unwrap_or("");
    let mut sim = state.sim.lock().unwrap_or_else(|e| e.into_inner());
    let amount = value
        .get("amount")
        .and_then(|v| v.as_str())
        .or_else(|| value.get("amount").and_then(|v| v.as_i64()).map(|_| ""))
        .unwrap_or("");
    let amount = if amount.is_empty() {
        value
            .get("amount")
            .map(|v| v.to_string().trim_matches('"').to_string())
            .unwrap_or_default()
    } else {
        amount.to_string()
    };
    match action {
        "set-initial-balance" | "set-profile-initial-balance" | "add-balance" => {
            let mint = value.get("mint").and_then(|v| v.as_str()).unwrap_or("USD");
            match sim.set_virtual_balance(&amount, mint) {
                Ok(()) => (200, serde_json::json!({"result": {"status": "OK"}, "report": paper_report(&sim)})),
                Err(err) => (400, serde_json::json!({"error": err.to_string()})),
            }
        }
        "initialize-profile-ledger" => {
            let id = value.get("profile_id").and_then(|v| v.as_str()).unwrap_or("");
            let store = state.profiles.lock().unwrap_or_else(|e| e.into_inner());
            match store.get(id) {
                Ok(Some(_)) => (200, serde_json::json!({"report": paper_report(&sim), "profile_id": id})),
                Ok(None) => (404, serde_json::json!({"error": "Profile not found."})),
                Err(err) => (500, serde_json::json!({"error": err.to_string()})),
            }
        }
        _ => (
            400,
            serde_json::json!({"error": "Action must be set-initial-balance, add-balance, set-profile-initial-balance, or initialize-profile-ledger."}),
        ),
    }
}

fn allow_request(hits: &Mutex<Vec<u64>>) -> bool {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mut hits = hits.lock().unwrap_or_else(|e| e.into_inner());
    hits.retain(|seen| now.saturating_sub(*seen) < 60);
    if hits.len() >= 120 {
        return false;
    }
    hits.push(now);
    true
}

fn authorized(request: &str, token: Option<&str>) -> bool {
    let Some(token) = token else {
        return true;
    };
    let expected = format!("Bearer {token}");
    request.lines().any(|line| {
        let mut parts = line.splitn(2, ':');
        matches!(parts.next(), Some(name) if name.eq_ignore_ascii_case("authorization"))
            && parts.next().map(str::trim) == Some(expected.as_str())
    })
}

fn html_index() -> String {
    let body = include_str!("../static/index.html");
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn respond(status: u16, payload: &serde_json::Value) -> String {
    let body = payload.to_string();
    let reason = match status {
        200 => "OK",
        201 => "Created",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        409 => "Conflict",
        429 => "Too Many Requests",
        _ => "Error",
    };
    format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

pub fn serve(addr: &str, state: PaperApiState) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let state = state.clone();
                thread::spawn(move || {
                    let _ = serve_one(stream, &state);
                });
            }
            Err(_) => continue,
        }
    }
    Ok(())
}

fn serve_one(mut stream: TcpStream, state: &PaperApiState) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut buf = [0u8; 16_384];
    let n = stream.read(&mut buf)?;
    let request = String::from_utf8_lossy(&buf[..n]);
    let response = handle_http(&request, state);
    stream.write_all(response.as_bytes())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_is_paper_only() {
        let state = PaperApiState::for_tests();
        let raw = handle_http("GET /health HTTP/1.1\r\nHost: x\r\n\r\n", &state);
        assert!(raw.contains("200"));
        assert!(raw.contains("\"real_trading\":false"));
        assert!(raw.contains("\"transaction_broadcast\":false"));
    }

    #[test]
    fn classify_route_buy() {
        let state = PaperApiState::for_tests();
        let body = r#"{"wallet":"Wallet11111111111111111111111111111111","token_balance_changes":[{"owner":"Wallet11111111111111111111111111111111","mint":"TokenMint111111111111111111111111111111","delta_amount":"10"},{"owner":"Wallet11111111111111111111111111111111","mint":"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v","delta_amount":"-4"}]}"#;
        let raw = handle_http(
            &format!("POST /api/classify HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{body}", body.len()),
            &state,
        );
        assert!(raw.contains("BUY_CANDIDATE"));
    }

    #[test]
    fn auth_required_when_token_set() {
        let mut state = PaperApiState::for_tests();
        state.api_token = Some("secret-token".into());
        let denied = handle_http("GET /api/paper HTTP/1.1\r\nHost: x\r\n\r\n", &state);
        assert!(denied.contains("401"));
        let ok = handle_http(
            "GET /api/paper HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer secret-token\r\n\r\n",
            &state,
        );
        assert!(ok.contains("200"));
    }

    #[test]
    fn dashboard_profile_contract() {
        let state = PaperApiState::for_tests();
        let created = handle_http(
            "POST /api/profiles HTTP/1.1\r\nHost: x\r\nContent-Length: 27\r\n\r\n{\"action\":\"create\",\"name\":\"alpha\"}",
            &state,
        );
        assert!(created.contains("201"), "{created}");
        assert!(created.contains("\"profile\""));
        let id = created
            .split("\"profile_id\":\"")
            .nth(1)
            .unwrap()
            .split('"')
            .next()
            .unwrap()
            .to_string();
        let body = format!("{{\"action\":\"update\",\"profile_id\":\"{id}\",\"status\":\"active\"}}");
        let updated = handle_http(
            &format!("POST /api/profiles HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{body}", body.len()),
            &state,
        );
        assert!(updated.contains("200"));
        assert!(updated.contains("\"status\":\"active\""));
    }

    #[test]
    fn dashboard_flow_create_fund_monitor() {
        let state = PaperApiState::for_tests();
        let wallet = "11111111111111111111111111111111";
        let created = handle_http(
            "POST /api/profiles HTTP/1.1\r\nHost: x\r\nContent-Length: 27\r\n\r\n{\"action\":\"create\",\"name\":\"alpha\"}",
            &state,
        );
        let id = created
            .split("\"profile_id\":\"")
            .nth(1)
            .unwrap()
            .split('"')
            .next()
            .unwrap();
        let add = format!("{{\"action\":\"add\",\"address\":\"{wallet}\"}}");
        let added = handle_http(
            &format!("POST /api/wallets HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{add}", add.len()),
            &state,
        );
        assert!(added.contains("200"), "{added}");
        let assoc = format!("{{\"action\":\"associate-source-wallet\",\"profile_id\":\"{id}\",\"wallet_address\":\"{wallet}\"}}");
        let associated = handle_http(
            &format!("POST /api/profiles HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{assoc}", assoc.len()),
            &state,
        );
        assert!(associated.contains("200"), "{associated}");
        let activate = format!("{{\"action\":\"update\",\"profile_id\":\"{id}\",\"status\":\"active\"}}");
        assert!(handle_http(
            &format!("POST /api/profiles HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{activate}", activate.len()),
            &state,
        )
        .contains("200"));
        let fund = format!("{{\"action\":\"set-profile-initial-balance\",\"profile_id\":\"{id}\",\"amount\":\"100\"}}");
        let funded = handle_http(
            &format!("POST /api/paper-simulation HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{fund}", fund.len()),
            &state,
        );
        assert!(funded.contains("\"available_balance\":\"100\""), "{funded}");
        let start = format!("{{\"action\":\"start\",\"profile_id\":\"{id}\"}}");
        let started = handle_http(
            &format!("POST /api/profile-monitor HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{start}", start.len()),
            &state,
        );
        assert!(started.contains("\"enabled\":true"), "{started}");
        let status = handle_http(
            &format!("GET /api/profile-monitor?profile_id={id} HTTP/1.1\r\nHost: x\r\n\r\n"),
            &state,
        );
        assert!(status.contains("\"enabled\":true"), "{status}");
        assert!(status.contains("\"transaction_broadcast\":false"));
    }

    #[test]
    fn process_fills_once_and_dedupes_in_sqlite() {
        let state = PaperApiState::for_tests();
        state.sim.lock().unwrap().set_virtual_balance("100", "USD").unwrap();
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
        let wallet = "11111111111111111111111111111111";
        let body = format!(
            r#"{{"wallet":"{wallet}","signature":"sig-process-1","block_time":{now},"confirmation_status":"finalized","target_notional_usd":"10","price_usd":"2","liquidity_usd":"20000","token_balance_changes":[{{"owner":"{wallet}","mint":"TokenMint111111111111111111111111111111","delta_amount":"1000"}},{{"owner":"{wallet}","mint":"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v","delta_amount":"-500"}}]}}"#
        );
        let first = handle_http(
            &format!("POST /api/process HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{body}", body.len()),
            &state,
        );
        assert!(first.contains("PROCESSED"), "{first}");
        assert!(first.contains("FILLED"), "{first}");
        assert!(first.contains("\"transaction_broadcast\":false"));
        let second = handle_http(
            &format!("POST /api/process HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{body}", body.len()),
            &state,
        );
        assert!(second.contains("DUPLICATE"), "{second}");
        assert!(state.signatures.lock().unwrap().is_processed(wallet, "sig-process-1").unwrap());
        let history = handle_http("GET /api/profile-transactions HTTP/1.1\r\nHost: x\r\n\r\n", &state);
        assert!(history.contains("BUY_CANDIDATE"), "{history}");
    }
}
