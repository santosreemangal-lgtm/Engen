//! Tiny paper HTTP API. No keys, no signing, no broadcast.
//!
//! GET  /health
//! GET  /api/paper
//! POST /api/candidates   JSON array of MonitorCandidate
//! POST /api/fill         JSON { "mint": "...", "price_usd": "2", "liquidity_usd": "20000" }

use crate::monitor_service::MonitorService;
use crate::paper_copy::MonitorCandidate;
use crate::paper_simulator::{MarketPrice, PaperSimulator};
use rust_decimal::Decimal;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

#[derive(Clone)]
pub struct PaperApiState {
    pub sim: Arc<Mutex<PaperSimulator>>,
    pub monitor: Arc<Mutex<MonitorService>>,
}

pub fn handle_http(request: &str, state: &PaperApiState) -> String {
    let mut lines = request.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("/");
    let body = request.split("\r\n\r\n").nth(1).unwrap_or("");

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
        ("GET", "/api/paper") => {
            let sim = state.sim.lock().unwrap_or_else(|e| e.into_inner());
            (200, sim.report())
        }
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

fn html_index() -> String {
    let body = r#"<!doctype html><html><head><meta charset="utf-8"><title>Engen paper</title>
<style>body{font:15px system-ui;background:#0b1020;color:#edf2ff;margin:32px}code{color:#9cf}</style>
</head><body>
<h1>Engen paper runtime</h1>
<p>Paper only. No signing. No broadcast.</p>
<p>GET <code>/health</code> · GET <code>/api/paper</code> · POST <code>/api/candidates</code> · POST <code>/api/fill</code></p>
<pre id="out">loading…</pre>
<script>
fetch('/api/paper').then(r=>r.json()).then(j=>{document.getElementById('out').textContent=JSON.stringify(j,null,2)}).catch(e=>{document.getElementById('out').textContent=String(e)})
</script></body></html>"#;
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn respond(status: u16, payload: &serde_json::Value) -> String {
    let body = payload.to_string();
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
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
        let state = PaperApiState {
            sim: Arc::new(Mutex::new(PaperSimulator::new())),
            monitor: Arc::new(Mutex::new(MonitorService::new())),
        };
        let raw = handle_http("GET /health HTTP/1.1\r\nHost: x\r\n\r\n", &state);
        assert!(raw.contains("200"));
        assert!(raw.contains("\"real_trading\":false"));
        assert!(raw.contains("\"transaction_broadcast\":false"));
    }
}
