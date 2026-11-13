//! Rust-native localhost dashboard HTTP/API runtime.
//!
//! This module deliberately uses only the Rust standard library for HTTP serving.
//! All chain access is public/read-only and all order execution is virtual paper-only.

use std::collections::{HashMap, VecDeque};
use std::error::Error;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use bigdecimal::BigDecimal;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Map, Value};
use uuid::Uuid;

use crate::config::Config;
use crate::market_data::fetch_market_price;
use crate::monitor_service::MonitorService;
use crate::paper_copy_runner::{
    execute_pending_paper_orders_with_policy, get_pending_copy_status,
    queue_paper_orders_with_policy, report_with_verified_market_prices, AllowedSides,
    CopySettings,
};
use crate::paper_simulator::{CreateOrderRequest, ExecutionPolicy, PaperSimulator};
use crate::pnl::MarketPrice;
use crate::profile_store::{ProfileStore, SaveTransaction};
use crate::signature_store::SignatureStore;
use crate::solana_public::{RpcConfig, SolanaRpcClient, DEFAULT_RPC_URL};
use crate::solana_validation::is_valid_solana_address;
use crate::transaction_monitor::monitor_wallet;

const MAX_BODY_BYTES: usize = 10_000;
const MAX_HEADERS_BYTES: usize = 16_384;
const MAX_HISTORY_PAGE_SIZE: usize = 100;
const MONITOR_INTERVAL_SECONDS: u64 = 30;
const MONITOR_SCAN_LIMIT: usize = 50;

#[derive(Debug)]
struct HttpRequest {
    method: String,
    target: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
    peer_ip: IpAddr,
}

#[derive(Debug)]
struct HttpResponse {
    status: u16,
    content_type: &'static str,
    body: Vec<u8>,
}

impl HttpResponse {
    fn json(status: u16, value: Value) -> Self {
        Self {
            status,
            content_type: "application/json; charset=utf-8",
            body: serde_json::to_vec(&value).unwrap_or_else(|_| b"{\"error\":\"Serialization failed.\"}".to_vec()),
        }
    }

    fn text(status: u16, content_type: &'static str, body: Vec<u8>) -> Self {
        Self { status, content_type, body }
    }
}

#[derive(Clone)]
struct ScanContext {
    project_root: PathBuf,
    profile_store: Arc<ProfileStore>,
    signature_store: Arc<SignatureStore>,
    rpc: Arc<SolanaRpcClient>,
    profile_simulators: Arc<Mutex<HashMap<String, Arc<Mutex<PaperSimulator>>>>>,
    execution_policy: ExecutionPolicy,
}

impl ScanContext {
    fn profile_simulator(&self, profile_id: &str) -> Result<Arc<Mutex<PaperSimulator>>, String> {
        let canonical = canonical_profile_id(profile_id)?;
        let mut cache = self.profile_simulators.lock().map_err(|_| "Paper simulator state unavailable.".to_string())?;
        if let Some(existing) = cache.get(&canonical) {
            return Ok(Arc::clone(existing));
        }
        let path = self.project_root.join("data").join("profiles").join(&canonical).join("paper_trading.db");
        let simulator = PaperSimulator::open(path, &std::collections::BTreeMap::new())
            .map_err(|_| "Profile paper ledger is unavailable.".to_string())?;
        let simulator = Arc::new(Mutex::new(simulator));
        cache.insert(canonical, Arc::clone(&simulator));
        Ok(simulator)
    }

    fn scan_wallet(&self, wallet: &str, profile_id: Option<&str>) -> Result<Vec<Value>, String> {
        let mut results = monitor_wallet(
            &self.rpc,
            &self.signature_store,
            wallet,
            MONITOR_SCAN_LIMIT,
            true,
            None,
        )
        .map_err(|_| "Read-only wallet scan failed; retry is scheduled.".to_string())?;

        if let Some(profile_id) = profile_id {
            self.persist_profile_observations(profile_id, wallet, &results)?;
            let profile = self.profile_store.get_profile(profile_id)
                .map_err(|_| "Profile store is unavailable.".to_string())?
                .ok_or_else(|| "Profile not found.".to_string())?;
            let paper_settings = profile.settings.get("paper_trading").cloned().unwrap_or_else(|| json!({}));
            let allowed_sides = paper_settings.get("copy_side")
                .and_then(Value::as_str).unwrap_or("BOTH");
            let allowed_sides = AllowedSides::parse(allowed_sides)
                .map_err(|_| "Paper copy-side settings are invalid.".to_string())?;
            let copy_settings = CopySettings::from_json(Some(&paper_settings))
                .map_err(|_| "Paper copy settings are invalid.".to_string())?;
            let simulator = self.profile_simulator(profile_id)?;
            let mut simulator = simulator.lock().map_err(|_| "Paper simulator state unavailable.".to_string())?;

            // Only results explicitly processed in the Fresh lane can create paper orders.
            // Historical backfill results remain visible and auditable but cannot trade.
            let fresh_results: Vec<Value> = results.iter()
                .filter(|item| item.get("source_lane").and_then(Value::as_str) == Some("fresh"))
                .cloned()
                .collect();

            let queued = queue_paper_orders_with_policy(
                wallet,
                &fresh_results,
                &mut simulator,
                allowed_sides,
                &copy_settings,
                None,
                &self.execution_policy,
            ).map_err(|_| "Paper-copy queue failed; eligible signatures remain retryable.".to_string())?;

            let outcomes = execute_pending_paper_orders_with_policy(
                &mut simulator,
                Some(wallet),
                |mint| fetch_market_price(mint, Duration::from_secs(5)).ok().flatten(),
                None,
                &self.execution_policy,
            );
            let outcome_by_signal: HashMap<String, Value> = outcomes.iter()
                .filter_map(|item| item.get("signal_id").and_then(Value::as_str)
                    .map(|signal| (signal.to_string(), item.clone())))
                .collect();
            let queued_by_signature: HashMap<String, Value> = queued.iter()
                .filter_map(|item| item.get("signature").and_then(Value::as_str)
                    .map(|signature| (signature.to_string(), item.clone())))
                .collect();

            for item in &results {
                let signature = item.get("signature").and_then(Value::as_str);
                if let Some(signature) = signature {
                    if item.get("retryable").and_then(Value::as_bool) != Some(true) {
                        let _ = self.signature_store.mark_processed(wallet, signature);
                    }
                }
            }

            for item in &mut results {
                if let Some(signature) = item.get("signature").and_then(Value::as_str) {
                    if let Some(order) = queued_by_signature.get(signature) {
                        let mut order = order.clone();
                        if let Some(signal_id) = order.get("signal_id").and_then(Value::as_str) {
                            if let Some(outcome) = outcome_by_signal.get(signal_id) {
                                order["execution_status"] = outcome.get("status").cloned().unwrap_or(Value::Null);
                            }
                        }
                        item["paper_copy_order"] = order;
                    }
                }
            }
            let pending = get_pending_copy_status(&simulator, Some(wallet));
            for item in &mut results {
                item["paper_pending_count"] = pending.get("pending_count").cloned().unwrap_or(json!(0));
            }
        } else {
            // Global monitoring is observation-only. It never queues or executes orders.
            for item in &results {
                if item.get("retryable").and_then(Value::as_bool) != Some(true) {
                    if let Some(signature) = item.get("signature").and_then(Value::as_str) {
                        let _ = self.signature_store.mark_processed(wallet, signature);
                    }
                }
            }
        }
        Ok(results)
    }

    fn persist_profile_observations(&self, profile_id: &str, wallet: &str, results: &[Value]) -> Result<(), String> {
        for item in results {
            let Some(signature) = item.get("signature").and_then(Value::as_str) else { continue };
            let signal = item.get("trade_signal").unwrap_or(&Value::Null);
            let classification = signal.get("classification").and_then(Value::as_str).unwrap_or("UNKNOWN");
            let classification = if matches!(classification, "BUY_CANDIDATE" | "SELL_CANDIDATE" | "TRANSFER" | "UNKNOWN" | "FAILED_TRANSACTION" | "RENT" | "LIQUIDITY_OPERATION") {
                classification
            } else { "UNKNOWN" };
            let observation = item.get("signature_observation").unwrap_or(&Value::Null);
            let confirmation = observation.get("confirmation_status").and_then(Value::as_str);
            let status = if item.get("status").and_then(Value::as_str) == Some("failed") {
                "failed"
            } else if matches!(confirmation, Some("confirmed" | "finalized")) && item.get("metadata_complete").and_then(Value::as_bool) == Some(true) {
                "confirmed"
            } else if item.get("retryable").and_then(Value::as_bool) == Some(true) || item.get("metadata_complete").and_then(Value::as_bool) == Some(false) {
                "pending"
            } else if item.get("status").and_then(Value::as_str) == Some("success") {
                "observed"
            } else { "unknown" };

            let slot = observation.get("slot").and_then(Value::as_i64).or_else(|| item.get("slot").and_then(Value::as_i64));
            let block_time = observation.get("block_time").and_then(Value::as_i64).or_else(|| item.get("block_time").and_then(Value::as_i64));
            let mut token_mint = None;
            let mut token_decimals = None;
            let mut raw_amount = None;
            if let Some(changes) = item.get("token_balance_changes").and_then(Value::as_array) {
                let candidate_mints = signal.get("asset_mints").and_then(Value::as_array);
                for change in changes {
                    let mint = change.get("mint").and_then(Value::as_str);
                    let decimals = change.get("decimals").and_then(Value::as_i64);
                    let delta = change.get("delta_amount").and_then(Value::as_str);
                    let (Some(mint), Some(decimals), Some(delta)) = (mint, decimals, delta) else { continue };
                    if decimals < 0 || decimals > 30 || delta.is_empty() { continue; }
                    let magnitude = delta.trim_start_matches('-');
                    if magnitude.is_empty() || !magnitude.bytes().all(|byte| byte.is_ascii_digit()) { continue; }
                    if token_mint.is_none() || candidate_mints.is_some_and(|items| items.iter().any(|v| v.as_str() == Some(mint))) {
                        token_mint = Some(mint.to_string());
                        token_decimals = Some(decimals);
                        raw_amount = Some(magnitude.to_string());
                        if candidate_mints.is_some_and(|items| items.iter().any(|v| v.as_str() == Some(mint))) { break; }
                    }
                }
            }
            let evidence = json!({
                "metadata_status": item.get("metadata_status").cloned().unwrap_or(json!("available")),
                "confirmation_status": confirmation,
                "classification_reason": signal.get("reason").cloned().unwrap_or(json!("")),
                "confidence": signal.get("confidence").cloned().unwrap_or(json!("unknown")),
                "monitored_wallet_is_signer": item.get("signers").and_then(Value::as_array)
                    .is_some_and(|signers| signers.iter().any(|value| value.as_str() == Some(wallet))),
                "real_trading": false,
                "transaction_broadcast": false
            });
            let profile_ids = vec![profile_id.to_string()];
            self.profile_store.save_transaction(SaveTransaction {
                signature,
                source_wallet_address: wallet,
                classification,
                status,
                evidence: Some(evidence),
                slot,
                block_time,
                raw_amount: raw_amount.as_deref(),
                token_mint: token_mint.as_deref(),
                token_decimals,
                profile_ids: &profile_ids,
                observed_at: None,
            }).map_err(|_| "Profile transaction history could not be saved.".to_string())?;
        }
        Ok(())
    }
}

struct DashboardApp {
    project_root: PathBuf,
    host: String,
    port: u16,
    auth_token: Option<String>,
    wallets_file: PathBuf,
    monitor_state_file: PathBuf,
    context: ScanContext,
    global_paper: Arc<Mutex<PaperSimulator>>,
    global_monitor: Mutex<Option<Arc<MonitorService>>>,
    profile_monitors: Mutex<HashMap<String, Arc<MonitorService>>>,
    rate_limit: Mutex<HashMap<IpAddr, VecDeque<Instant>>>,
}

impl DashboardApp {
    fn from_environment() -> Result<Arc<Self>, Box<dyn Error>> {
        let project_root = std::env::var_os("ENGEN_APP_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap_or(Path::new(".")).to_path_buf());
        let data_dir = project_root.join("data");
        fs::create_dir_all(&data_dir)?;

        let profile_db = std::env::var_os("PROFILE_STORE_DB").map(PathBuf::from)
            .unwrap_or_else(|| data_dir.join("profiles.db"));
        let signature_db = std::env::var_os("SIGNATURE_DB").map(PathBuf::from)
            .unwrap_or_else(|| data_dir.join("signatures.db"));
        let paper_db = std::env::var_os("PAPER_SIMULATOR_DB").map(PathBuf::from)
            .unwrap_or_else(|| data_dir.join("paper_simulator.db"));
        let wallets_file = std::env::var_os("WALLETS_FILE").map(PathBuf::from)
            .unwrap_or_else(|| project_root.join("dashboard").join("wallets.json"));
        let monitor_state_file = std::env::var_os("MONITOR_STATE_FILE").map(PathBuf::from)
            .unwrap_or_else(|| data_dir.join("monitoring_state.json"));

        let config = Config::from_process_env()?;
        let rpc = SolanaRpcClient::new(RpcConfig {
            endpoint: config.rpc_url.clone().unwrap_or_else(|| DEFAULT_RPC_URL.to_string()),
            timeout: Duration::from_secs_f64(config.rpc_timeout_seconds),
            retries: config.rpc_retries,
            backoff: Duration::from_secs_f64(config.rpc_backoff_seconds),
            min_interval: Duration::from_secs_f64(config.rpc_min_interval_seconds),
        })?;
        let profile_store = Arc::new(ProfileStore::open(profile_db)?);
        let signature_store = Arc::new(SignatureStore::open(signature_db)?);
        let global_paper = PaperSimulator::open(paper_db, &std::collections::BTreeMap::new())?;
        let context = ScanContext {
            project_root: project_root.clone(),
            profile_store,
            signature_store,
            rpc: Arc::new(rpc),
            profile_simulators: Arc::new(Mutex::new(HashMap::new())),
            execution_policy: config.paper_execution_policy(),
        };

        let host = std::env::var("DASHBOARD_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
        let port = std::env::var("PORT").unwrap_or_else(|_| "8000".to_string()).parse::<u16>()?;
        let token = std::env::var("DASHBOARD_AUTH_TOKEN").ok().filter(|value| !value.is_empty());
        let trust_proxy = std::env::var("DASHBOARD_TRUST_PROXY").unwrap_or_default().eq_ignore_ascii_case("true");
        if token.as_ref().is_some_and(|value| value.len() < 32) {
            return Err("DASHBOARD_AUTH_TOKEN must contain at least 32 characters.".into());
        }
        if !matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1") && (!trust_proxy || token.is_none()) {
            return Err("Non-localhost binding requires DASHBOARD_TRUST_PROXY=true and a strong DASHBOARD_AUTH_TOKEN.".into());
        }

        Ok(Arc::new(Self {
            project_root,
            host,
            port,
            auth_token: token,
            wallets_file,
            monitor_state_file,
            context,
            global_paper: Arc::new(Mutex::new(global_paper)),
            global_monitor: Mutex::new(None),
            profile_monitors: Mutex::new(HashMap::new()),
            rate_limit: Mutex::new(HashMap::new()),
        }))
    }

    fn canonical_profile(profile_id: &str) -> Result<String, HttpResponse> {
        canonical_profile_id(profile_id).map_err(|message| HttpResponse::json(400, json!({"error":message})))
    }

    fn global_monitor_service(&self) -> Result<Arc<MonitorService>, HttpResponse> {
        let mut service = self.global_monitor.lock().map_err(|_| HttpResponse::json(503, json!({"error":"Monitor service unavailable."})))?;
        if let Some(existing) = service.as_ref() { return Ok(Arc::clone(existing)); }
        let context = self.context.clone();
        let created = MonitorService::new(MONITOR_INTERVAL_SECONDS, move |wallet| context.scan_wallet(wallet, None))
            .map_err(|_| HttpResponse::json(503, json!({"error":"Monitor service could not start."})))?;
        let created = Arc::new(created);
        *service = Some(Arc::clone(&created));
        Ok(created)
    }

    fn profile_monitor_service(&self, profile_id: &str) -> Result<Arc<MonitorService>, HttpResponse> {
        let mut services = self.profile_monitors.lock().map_err(|_| HttpResponse::json(503, json!({"error":"Profile monitor state unavailable."})))?;
        if let Some(existing) = services.get(profile_id) { return Ok(Arc::clone(existing)); }
        let context = self.context.clone();
        let id = profile_id.to_string();
        let created = MonitorService::new(MONITOR_INTERVAL_SECONDS, move |wallet| context.scan_wallet(wallet, Some(&id)))
            .map_err(|_| HttpResponse::json(503, json!({"error":"Profile monitor service could not start."})))?;
        let created = Arc::new(created);
        services.insert(profile_id.to_string(), Arc::clone(&created));
        Ok(created)
    }

    fn wallets(&self) -> Vec<String> {
        fs::read_to_string(&self.wallets_file).ok()
            .and_then(|raw| serde_json::from_str::<Vec<String>>(&raw).ok())
            .unwrap_or_default()
            .into_iter()
            .filter(|address| is_valid_solana_address(address))
            .take(100)
            .collect()
    }

    fn save_wallets(&self, wallets: &[String]) -> Result<(), HttpResponse> {
        let parent = self.wallets_file.parent().ok_or_else(|| HttpResponse::json(503, json!({"error":"Wallet storage path is invalid."})))?;
        fs::create_dir_all(parent).map_err(|_| HttpResponse::json(503, json!({"error":"Wallet storage unavailable."})))?;
        let temporary = self.wallets_file.with_extension("tmp");
        let data = serde_json::to_vec_pretty(wallets).map_err(|_| HttpResponse::json(500, json!({"error":"Wallet storage serialization failed."})))?;
        fs::write(&temporary, data).map_err(|_| HttpResponse::json(503, json!({"error":"Wallet storage unavailable."})))?;
        fs::rename(temporary, &self.wallets_file).map_err(|_| HttpResponse::json(503, json!({"error":"Wallet storage unavailable."})))?;
        Ok(())
    }

    fn monitor_prefs(&self) -> Value {
        fs::read_to_string(&self.monitor_state_file).ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({"global_enabled":false,"profile_ids":[]}))
    }

    fn save_monitor_prefs(&self, prefs: &Value) -> Result<(), HttpResponse> {
        let parent = self.monitor_state_file.parent().ok_or_else(|| HttpResponse::json(503, json!({"error":"Monitor state path is invalid."})))?;
        fs::create_dir_all(parent).map_err(|_| HttpResponse::json(503, json!({"error":"Monitor state storage unavailable."})))?;
        let temporary = self.monitor_state_file.with_extension("tmp");
        let data = serde_json::to_vec_pretty(prefs).map_err(|_| HttpResponse::json(500, json!({"error":"Monitor state serialization failed."})))?;
        fs::write(&temporary, data).map_err(|_| HttpResponse::json(503, json!({"error":"Monitor state storage unavailable."})))?;
        fs::rename(temporary, &self.monitor_state_file).map_err(|_| HttpResponse::json(503, json!({"error":"Monitor state storage unavailable."})))?;
        Ok(())
    }

    fn set_profile_monitor_pref(&self, profile_id: &str, enabled: bool) -> Result<(), HttpResponse> {
        let mut prefs = self.monitor_prefs();
        if !prefs["profile_ids"].is_array() { prefs["profile_ids"] = json!([]); }
        let ids = prefs["profile_ids"].as_array_mut().unwrap();
        ids.retain(|value| value.as_str() != Some(profile_id));
        if enabled { ids.push(json!(profile_id)); }
        self.save_monitor_prefs(&prefs)
    }

    fn restore_monitors(&self) {
        let prefs = self.monitor_prefs();
        if prefs.get("global_enabled").and_then(Value::as_bool) == Some(true) {
            let wallets = self.wallets();
            if !wallets.is_empty() {
                if let Ok(service) = self.global_monitor_service() {
                    let _ = service.start(&wallets);
                }
            }
        }
        if let Some(ids) = prefs.get("profile_ids").and_then(Value::as_array) {
            let mut restored = Vec::new();
            for value in ids {
                let Some(id) = value.as_str() else { continue };
                let Ok(id) = canonical_profile_id(id) else { continue };
                let Ok(Some(profile)) = self.context.profile_store.get_profile(&id) else { continue };
                if profile.status != "active" || profile.source_wallets.is_empty() { continue; }
                if let Ok(service) = self.profile_monitor_service(&id) {
                    if service.start(&profile.source_wallets).is_ok() { restored.push(json!(id)); }
                }
            }
            let mut next = prefs;
            next["profile_ids"] = json!(restored);
            let _ = self.save_monitor_prefs(&next);
        }
    }

    fn rate_limited(&self, ip: IpAddr) -> bool {
        let Ok(mut guard) = self.rate_limit.lock() else { return true };
        let now = Instant::now();
        let requests = guard.entry(ip).or_default();
        while requests.front().is_some_and(|instant| now.duration_since(*instant) > Duration::from_secs(60)) {
            requests.pop_front();
        }
        if requests.len() >= 120 { return true; }
        requests.push_back(now);
        false
    }

    fn api_authorized(&self, request: &HttpRequest) -> bool {
        let Some(expected) = self.auth_token.as_deref() else { return true };
        let supplied = request.headers.get("authorization").map(String::as_str).unwrap_or("");
        let Some(token) = supplied.strip_prefix("Bearer ") else { return false };
        constant_time_eq(token.as_bytes(), expected.as_bytes())
    }

    fn handle(&self, request: HttpRequest) -> HttpResponse {
        let parsed = match url::Url::parse(&format!("http://localhost{}", request.target)) {
            Ok(url) => url,
            Err(_) => return HttpResponse::json(400, json!({"error":"Invalid request target."})),
        };
        let path = parsed.path().to_string();
        if path.starts_with("/api/") {
            if self.rate_limited(request.peer_ip) {
                return HttpResponse::json(429, json!({"error":"API request limit reached. Wait before retrying."}));
            }
            if !self.api_authorized(&request) {
                return HttpResponse::json(401, json!({"error":"Dashboard API authentication required."}));
            }
            if request.method == "POST" {
                if let Some(origin) = request.headers.get("origin") {
                    let host = request.headers.get("host").map(String::as_str).unwrap_or("");
                    let same = origin.strip_prefix("http://").or_else(|| origin.strip_prefix("https://"))
                        .is_some_and(|value| value.eq_ignore_ascii_case(host));
                    if !same {
                        return HttpResponse::json(403, json!({"error":"Cross-origin state changes are not allowed."}));
                    }
                }
            }
            let body: Value = if request.body.is_empty() { json!({}) } else {
                match serde_json::from_slice(&request.body) {
                    Ok(value) if value.is_object() => value,
                    _ => return HttpResponse::json(400, json!({"error":"Request body must be a JSON object."})),
                }
            };
            let query: HashMap<String, String> = parsed.query().map(|value|
                url::form_urlencoded::parse(value.as_bytes()).into_owned().collect()
            ).unwrap_or_default();
            return match request.method.as_str() {
                "GET" => self.get_api(&path, &query),
                "POST" => self.post_api(&path, &body),
                _ => HttpResponse::json(405, json!({"error":"Method not allowed."})),
            };
        }

        if request.method != "GET" {
            return HttpResponse::text(405, "text/plain; charset=utf-8", b"Method not allowed".to_vec());
        }
        let relative = match path.as_str() {
            "/" | "/index.html" => PathBuf::from("index.html"),
            _ => match path.strip_prefix("/dashboard/") {
                Some(value) => PathBuf::from(value),
                None => return HttpResponse::text(404, "text/plain; charset=utf-8", b"Not found".to_vec()),
            },
        };
        if relative.components().any(|part| !matches!(part, std::path::Component::Normal(_))) {
            return HttpResponse::text(400, "text/plain; charset=utf-8", b"Invalid path".to_vec());
        }
        let full_path = self.project_root.join("dashboard").join(relative);
        match fs::read(&full_path) {
            Ok(bytes) => {
                let content_type = match full_path.extension().and_then(|value| value.to_str()).unwrap_or("") {
                    "html" => "text/html; charset=utf-8",
                    "css" => "text/css; charset=utf-8",
                    "js" => "application/javascript; charset=utf-8",
                    "svg" => "image/svg+xml",
                    "png" => "image/png",
                    "ico" => "image/x-icon",
                    _ => "application/octet-stream",
                };
                HttpResponse::text(200, content_type, bytes)
            }
            Err(_) => HttpResponse::text(404, "text/plain; charset=utf-8", b"Not found".to_vec()),
        }
    }

    fn get_api(&self, path: &str, query: &HashMap<String, String>) -> HttpResponse {
        match path {
            "/api/wallets" => HttpResponse::json(200, json!({"wallets":self.wallets()})),
            "/api/monitor" => {
                let status = self.global_monitor.lock().ok().and_then(|value| value.as_ref().map(|service| service.status().ok())).flatten()
                    .unwrap_or_else(|| json!({"enabled":false,"interval_seconds":MONITOR_INTERVAL_SECONDS,"wallets":[]}));
                HttpResponse::json(200, status)
            }
            "/api/rpc-health" => {
                let checked_at = Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true);
                match self.context.rpc.get_health() {
                    Ok(status) => HttpResponse::json(200, json!({"mode":"READ_ONLY","available":true,"status":status,"endpoint":"configured public Solana RPC","checked_at":checked_at,"transaction_broadcast":false})),
                    Err(error) => HttpResponse::json(200, json!({"mode":"READ_ONLY","available":false,"status":"unavailable","endpoint":"configured public Solana RPC","checked_at":checked_at,"error":error.to_string(),"transaction_broadcast":false})),
                }
            }
            "/api/profiles" => self.profiles_report(),
            "/api/profile-transactions" => self.profile_transactions(query),
            "/api/profile-monitor" => {
                let Some(profile_id) = query.get("profile_id") else {
                    return HttpResponse::json(400, json!({"error":"A valid profile_id is required."}));
                };
                let profile_id = match Self::canonical_profile(profile_id) { Ok(value) => value, Err(response) => return response };
                let profile = match self.context.profile_store.get_profile(&profile_id) {
                    Ok(Some(profile)) => profile,
                    Ok(None) => return HttpResponse::json(404, json!({"error":"Profile not found."})),
                    Err(_) => return HttpResponse::json(503, json!({"error":"Profile store unavailable."})),
                };
                let monitor = self.profile_monitors.lock().ok()
                    .and_then(|services| services.get(&profile_id).cloned())
                    .and_then(|service| service.status().ok())
                    .unwrap_or_else(|| json!({"enabled":false,"interval_seconds":MONITOR_INTERVAL_SECONDS,"wallets":[]}));
                HttpResponse::json(200, json!({"profile_id":profile_id,"profile_status":profile.status,"monitor":monitor}))
            }
            "/api/paper-simulation" => self.paper_report(query.get("profile_id").map(String::as_str)),
            _ => HttpResponse::json(404, json!({"error":"Endpoint not found."})),
        }
    }

    fn post_api(&self, path: &str, body: &Value) -> HttpResponse {
        match path {
            "/api/wallets" => {
                let Some(address) = body.get("address").and_then(Value::as_str) else {
                    return HttpResponse::json(400, json!({"error":"Invalid-looking Solana address"}));
                };
                if !is_valid_solana_address(address.trim()) {
                    return HttpResponse::json(400, json!({"error":"Invalid-looking Solana address"}));
                }
                let address = address.trim().to_string();
                let action = body.get("action").and_then(Value::as_str).unwrap_or("add");
                let mut wallets = self.wallets();
                match action {
                    "add" => {
                        if !wallets.contains(&address) {
                            if wallets.len() >= 100 { return HttpResponse::json(413, json!({"error":"The saved-wallet limit has been reached."})); }
                            wallets.push(address);
                        }
                    }
                    "remove" => wallets.retain(|item| item != &address),
                    _ => return HttpResponse::json(400, json!({"error":"Action must be add or remove"})),
                }
                if let Err(response) = self.save_wallets(&wallets) { return response; }
                HttpResponse::json(200, json!({"wallets":wallets}))
            }
            "/api/monitor" => {
                let action = body.get("action").and_then(Value::as_str).unwrap_or("");
                let wallets = self.wallets();
                match action {
                    "start" => {
                        if wallets.is_empty() { return HttpResponse::json(400, json!({"error":"Add at least one wallet before starting."})); }
                        let service = match self.global_monitor_service() { Ok(value) => value, Err(response) => return response };
                        if let Err(error) = service.start(&wallets) { return HttpResponse::json(400, json!({"error":error.to_string()})); }
                        let mut prefs = self.monitor_prefs();
                        prefs["global_enabled"] = json!(true);
                        if let Err(response) = self.save_monitor_prefs(&prefs) { let _ = service.stop(); return response; }
                        HttpResponse::json(200, service.status().unwrap_or_else(|_| json!({"enabled":true,"wallets":[]})))
                    }
                    "stop" => {
                        let service = match self.global_monitor_service() { Ok(value) => value, Err(response) => return response };
                        let _ = service.stop();
                        let mut prefs = self.monitor_prefs();
                        prefs["global_enabled"] = json!(false);
                        if let Err(response) = self.save_monitor_prefs(&prefs) { return response; }
                        HttpResponse::json(200, service.status().unwrap_or_else(|_| json!({"enabled":false,"wallets":[]})))
                    }
                    _ => HttpResponse::json(400, json!({"error":"Action must be start or stop"})),
                }
            }
            "/api/profile-monitor" => self.post_profile_monitor(body),
            "/api/profiles" => self.post_profiles(body),
            "/api/paper-simulation" => self.post_paper_simulation(body),
            _ => HttpResponse::json(404, json!({"error":"Endpoint not found"})),
        }
    }

    fn profiles_report(&self) -> HttpResponse {
        let profiles = match self.context.profile_store.list_profiles(false) {
            Ok(profiles) => profiles,
            Err(_) => return HttpResponse::json(503, json!({"database_available":false,"profiles":[],"error":"Profile report unavailable."})),
        };
        let mut values = Vec::new();
        for profile in profiles {
            let mut balances = Vec::new();
            if let Ok(snapshots) = self.context.profile_store.current_balances(&profile.profile_id) {
                for snapshot in snapshots {
                    let amount = BigDecimal::from_str(&snapshot.raw_amount).ok()
                        .zip(BigDecimal::from_str(&format!("1e-{}", snapshot.token_decimals)).ok())
                        .map(|(raw, factor)| (raw * factor).normalized().to_plain_string());
                    let age = chrono::DateTime::parse_from_rfc3339(&snapshot.observed_at).ok()
                        .map(|date| Utc::now().signed_duration_since(date.with_timezone(&Utc)).num_seconds().max(0));
                    balances.push(json!({
                        "wallet_address":snapshot.wallet_address,
                        "token_mint":snapshot.token_mint,
                        "raw_amount":snapshot.raw_amount,
                        "token_decimals":snapshot.token_decimals,
                        "amount":amount,
                        "observed_at":snapshot.observed_at,
                        "source":snapshot.source,
                        "freshness_seconds":snapshot.freshness_seconds,
                        "age_seconds":age,
                        "is_stale":age.zip(snapshot.freshness_seconds).map(|(age,limit)| age>limit),
                        "value_kind":"OBSERVED_ON_CHAIN_SNAPSHOT",
                        "simulated":false
                    }));
                }
            }
            values.push(json!({
                "profile_id":profile.profile_id,
                "name":profile.name,
                "destination_public_address":profile.destination_public_address,
                "status":profile.status,
                "created_at":profile.created_at,
                "updated_at":profile.updated_at,
                "settings":profile.settings,
                "source_wallets":profile.source_wallets,
                "balances":balances,
                "balances_source":"stored snapshots; not a live RPC refresh",
                "simulated":false
            }));
        }
        HttpResponse::json(200, json!({"database_available":true,"profiles":values}))
    }

    fn profile_transactions(&self, query: &HashMap<String, String>) -> HttpResponse {
        let Some(profile_id) = query.get("profile_id") else { return HttpResponse::json(400, json!({"error":"A valid profile_id is required."})); };
        let profile_id = match Self::canonical_profile(profile_id) { Ok(value) => value, Err(response) => return response };
        let page = match parse_positive(query.get("page"), 1, 1_000_000) { Ok(value) => value, Err(message) => return HttpResponse::json(400, json!({"error":message})) };
        let page_size = match parse_positive(query.get("page_size"), 25, MAX_HISTORY_PAGE_SIZE) { Ok(value) => value, Err(message) => return HttpResponse::json(400, json!({"error":message})) };
        let profile = match self.context.profile_store.get_profile(&profile_id) {
            Ok(Some(value)) => value,
            Ok(None) => return HttpResponse::json(404, json!({"error":"Profile not found."})),
            Err(_) => return HttpResponse::json(503, json!({"error":"Transaction history is temporarily unavailable."})),
        };
        let _ = profile;
        let rows = match self.context.profile_store.list_transactions(&profile_id) {
            Ok(rows) => rows,
            Err(_) => return HttpResponse::json(503, json!({"error":"Transaction history is temporarily unavailable."})),
        };
        let classification = query.get("classification").map(String::as_str).unwrap_or("");
        let status = query.get("status").map(String::as_str).unwrap_or("");
        let mint = query.get("token_mint").map(String::as_str).unwrap_or("");
        if !classification.is_empty() && !matches!(classification, "BUY_CANDIDATE" | "SELL_CANDIDATE" | "TRANSFER" | "UNKNOWN" | "FAILED_TRANSACTION" | "RENT" | "LIQUIDITY_OPERATION") {
            return HttpResponse::json(400, json!({"error":"Unsupported transaction classification."}));
        }
        if !status.is_empty() && !matches!(status, "observed" | "confirmed" | "failed" | "pending" | "unknown") {
            return HttpResponse::json(400, json!({"error":"Unsupported transaction status."}));
        }
        if !mint.is_empty() && !is_valid_solana_address(mint) {
            return HttpResponse::json(400, json!({"error":"Invalid token mint."}));
        }
        let filtered: Vec<Value> = rows.into_iter().filter(|row|
            (classification.is_empty() || row.classification == classification) &&
            (status.is_empty() || row.status == status) &&
            (mint.is_empty() || row.token_mint.as_deref() == Some(mint))
        ).map(|row| {
            let amount = row.raw_amount.as_deref().zip(row.token_decimals)
                .and_then(|(raw, decimals)| BigDecimal::from_str(raw).ok()
                    .zip(BigDecimal::from_str(&format!("1e-{}", decimals)).ok())
                    .map(|(raw, factor)| (raw * factor).normalized().to_plain_string()));
            json!({
                "signature":row.signature,
                "source_wallet_address":row.source_wallet_address,
                "slot":row.slot,
                "block_time":row.block_time,
                "observed_at":row.observed_at,
                "status":row.status,
                "classification":row.classification,
                "evidence":row.evidence,
                "raw_amount":row.raw_amount,
                "token_mint":row.token_mint,
                "token_decimals":row.token_decimals,
                "amount":amount,
                "value_kind":"OBSERVED_TRANSACTION_METADATA",
                "simulated":false
            })
        }).collect();
        let total = filtered.len();
        let start = (page - 1).saturating_mul(page_size).min(total);
        let end = start.saturating_add(page_size).min(total);
        let transactions = filtered[start..end].to_vec();
        HttpResponse::json(200, json!({
            "database_available":true,
            "transactions":transactions,
            "page":page,
            "page_size":page_size,
            "total":total,
            "pages":if total==0 {0} else {(total+page_size-1)/page_size}
        }))
    }

    fn post_profiles(&self, body: &Value) -> HttpResponse {
        let action = body.get("action").and_then(Value::as_str).unwrap_or("create");
        match action {
            "create" => {
                let name = body.get("name").and_then(Value::as_str).unwrap_or("");
                let destination = body.get("destination_public_address").and_then(Value::as_str);
                let settings = body.get("settings").cloned().unwrap_or_else(|| json!({}));
                match self.context.profile_store.create_profile(name, destination, Some(settings)) {
                    Ok(id) => {
                        let profile = self.context.profile_store.get_profile(&id).ok().flatten();
                        HttpResponse::json(201, json!({"profile":profile,"simulated":false}))
                    }
                    Err(error) => HttpResponse::json(400, json!({"error":error.to_string()})),
                }
            }
            "update" | "settings" | "associate-source-wallet" | "disassociate-source-wallet" | "delete" => {
                let Some(raw_id) = body.get("profile_id").and_then(Value::as_str) else { return HttpResponse::json(400, json!({"error":"A valid profile_id is required."})); };
                let id = match Self::canonical_profile(raw_id) { Ok(value) => value, Err(response) => return response };
                let result = match action {
                    "update" => {
                        let name = body.get("name").and_then(Value::as_str);
                        let destination = body.get("destination_public_address").and_then(Value::as_str);
                        let status = body.get("status").and_then(Value::as_str);
                        self.context.profile_store.update_profile(&id, name, destination, status)
                            .map_err(|error| error.to_string()).and_then(|_| {
                                if let Some(settings) = body.get("settings") {
                                    self.context.profile_store.set_settings(&id, settings).map_err(|error| error.to_string())?;
                                }
                                serde_json::to_value(self.context.profile_store.get_profile(&id).map_err(|error| error.to_string())?
                                    .ok_or_else(|| "Profile not found.".to_string())?).map_err(|_| "Profile response serialization failed.".to_string())
                            })
                    }
                    "settings" => {
                        let Some(settings) = body.get("settings") else { return HttpResponse::json(400, json!({"error":"Settings must be an object."})); };
                        self.context.profile_store.set_settings(&id, settings)
                            .map_err(|error| error.to_string())
                            .and_then(|_| serde_json::to_value(self.context.profile_store.get_profile(&id).map_err(|error| error.to_string())?.ok_or_else(|| "Profile not found.".to_string())?).map_err(|_| "Profile response serialization failed.".to_string()))
                    }
                    "associate-source-wallet" => {
                        let Some(wallet) = body.get("wallet_address").and_then(Value::as_str) else { return HttpResponse::json(400, json!({"error":"A valid public wallet address is required."})); };
                        self.context.profile_store.associate_source_wallet(&id, wallet).map_err(|error| error.to_string())
                            .and_then(|_| self.context.profile_store.get_profile(&id).map_err(|error| error.to_string())?.ok_or_else(|| "Profile not found.".to_string()))
                    }
                    "disassociate-source-wallet" => {
                        let Some(wallet) = body.get("wallet_address").and_then(Value::as_str) else { return HttpResponse::json(400, json!({"error":"A valid public wallet address is required."})); };
                        self.context.profile_store.disassociate_source_wallet(&id, wallet).map_err(|error| error.to_string())
                            .and_then(|_| self.context.profile_store.get_profile(&id).map_err(|error| error.to_string())?.ok_or_else(|| "Profile not found.".to_string()))
                    }
                    "delete" => {
                        if let Ok(mut services) = self.profile_monitors.lock() {
                            if let Some(service) = services.remove(&id) { let _ = service.stop_and_wait(Duration::from_secs(15)); }
                        }
                        let _ = self.set_profile_monitor_pref(&id, false);
                        self.context.profile_store.delete_profile(&id).map(|_| json!({"deleted":true,"profile_id":id,"history_preserved":true}))
                            .map_err(|error| error.to_string())
                    }
                    _ => unreachable!(),
                };
                match result {
                    Ok(profile) => HttpResponse::json(200, json!({"profile":profile,"simulated":false})),
                    Err(error) => {
                        let status = if error.contains("not found") || error.contains("Profile not found") {404} else {400};
                        HttpResponse::json(status, json!({"error":error}))
                    }
                }
            }
            _ => HttpResponse::json(400, json!({"error":"Action must be create, update, settings, associate-source-wallet, disassociate-source-wallet, or delete."})),
        }
    }

    fn post_profile_monitor(&self, body: &Value) -> HttpResponse {
        let Some(raw_id) = body.get("profile_id").and_then(Value::as_str) else { return HttpResponse::json(400, json!({"error":"A valid profile_id is required."})); };
        let id = match Self::canonical_profile(raw_id) { Ok(value) => value, Err(response) => return response };
        let profile = match self.context.profile_store.get_profile(&id) {
            Ok(Some(value)) => value,
            Ok(None) => return HttpResponse::json(404, json!({"error":"Profile not found."})),
            Err(_) => return HttpResponse::json(503, json!({"error":"Profile store unavailable."})),
        };
        let action = body.get("action").and_then(Value::as_str).unwrap_or("");
        match action {
            "start" => {
                if profile.status != "active" { return HttpResponse::json(409, json!({"error":"Activate this profile before starting monitoring."})); }
                if profile.source_wallets.is_empty() { return HttpResponse::json(400, json!({"error":"Associate at least one source public wallet with this profile first."})); }
                let service = match self.profile_monitor_service(&id) { Ok(value) => value, Err(response) => return response };
                if let Err(error) = service.start(&profile.source_wallets) { return HttpResponse::json(400, json!({"error":error.to_string()})); }
                if let Err(response) = self.set_profile_monitor_pref(&id, true) { let _ = service.stop(); return response; }
                HttpResponse::json(200, json!({"profile_id":id,"monitor":service.status().unwrap_or_else(|_| json!({"enabled":true}))}))
            }
            "stop" => {
                let status = self.profile_monitors.lock().ok().and_then(|services| services.get(&id).cloned())
                    .and_then(|service| service.stop().ok())
                    .unwrap_or_else(|| json!({"enabled":false,"interval_seconds":MONITOR_INTERVAL_SECONDS,"wallets":[]}));
                if let Err(response) = self.set_profile_monitor_pref(&id, false) { return response; }
                HttpResponse::json(200, json!({"profile_id":id,"monitor":status}))
            }
            _ => HttpResponse::json(400, json!({"error":"Action must be start or stop."})),
        }
    }

    fn paper_report(&self, profile_id: Option<&str>) -> HttpResponse {
        if let Some(profile_id) = profile_id {
            let id = match Self::canonical_profile(profile_id) { Ok(value) => value, Err(response) => return response };
            match self.context.profile_store.get_profile(&id) {
                Ok(Some(_)) => {}
                Ok(None) => return HttpResponse::json(404, json!({"error":"Profile not found."})),
                Err(_) => return HttpResponse::json(503, json!({"error":"Profile store unavailable."})),
            }
            let path = self.project_root.join("data").join("profiles").join(&id).join("paper_trading.db");
            if !path.is_file() {
                return HttpResponse::json(200, empty_paper_report(&id, false));
            }
            let simulator = match self.context.profile_simulator(&id) {
                Ok(value) => value,
                Err(error) => return HttpResponse::json(503, json!({"error":error})),
            };
            let simulator = match simulator.lock() {
                Ok(value) => value,
                Err(_) => return HttpResponse::json(503, json!({"error":"Paper ledger unavailable."})),
            };
            return match report_with_verified_market_prices(&simulator, Duration::from_secs(5)) {
                Ok(report) => HttpResponse::json(200, enrich_paper_report(report, &id)),
                Err(_) => HttpResponse::json(503, json!({"error":"Paper report unavailable."})),
            };
        }
        let simulator = match self.global_paper.lock() {
            Ok(value) => value,
            Err(_) => return HttpResponse::json(503, json!({"error":"Paper ledger unavailable."})),
        };
        match report_with_verified_market_prices(&simulator, Duration::from_secs(5)) {
            Ok(report) => HttpResponse::json(200, enrich_paper_report(report, "")),
            Err(_) => HttpResponse::json(503, json!({"error":"Paper report unavailable."})),
        }
    }

    fn post_paper_simulation(&self, body: &Value) -> HttpResponse {
        let action = body.get("action").and_then(Value::as_str).unwrap_or("");
        let profile_id = body.get("profile_id").and_then(Value::as_str);
        let mut profile_simulator = None;
        if let Some(raw_id) = profile_id {
            let id = match Self::canonical_profile(raw_id) { Ok(value) => value, Err(response) => return response };
            match self.context.profile_store.get_profile(&id) {
                Ok(Some(_)) => {}
                Ok(None) => return HttpResponse::json(404, json!({"error":"Profile not found."})),
                Err(_) => return HttpResponse::json(503, json!({"error":"Profile store unavailable."})),
            }
            match self.context.profile_simulator(&id) {
                Ok(simulator) => profile_simulator = Some((id, simulator)),
                Err(error) => return HttpResponse::json(503, json!({"error":error})),
            }
        }
        if action == "initialize-profile-ledger" {
            let Some((id, simulator)) = profile_simulator else { return HttpResponse::json(400, json!({"error":"profile_id is required."})); };
            let report = simulator.lock().ok().and_then(|sim| sim.report().ok()).unwrap_or_else(|| empty_paper_report(&id, true));
            return HttpResponse::json(200, json!({"profile_id":id,"database_available":true,"initialized":true,"report":report,"real_trading":false,"transaction_broadcast":false}));
        }
        if action == "set-profile-initial-balance" {
            let Some((id, simulator)) = profile_simulator else { return HttpResponse::json(400, json!({"error":"profile_id is required."})); };
            let amount = match body.get("amount").and_then(number_string) { Some(value) => value, None => return HttpResponse::json(400, json!({"error":"Enter a valid virtual balance amount."})) };
            let value = match BigDecimal::from_str(&amount) { Ok(value) if value > BigDecimal::from(0) && value <= BigDecimal::from(1_000_000_000) => value, _ => return HttpResponse::json(400, json!({"error":"Virtual balance must be greater than zero and at most 1,000,000,000 USD."})) };
            let mut simulator = match simulator.lock() { Ok(value) => value, Err(_) => return HttpResponse::json(503, json!({"error":"Paper ledger unavailable."})) };
            match simulator.set_virtual_balance(&value.normalized().to_plain_string(), "USD") {
                Ok(result) => {
                    let report = simulator.report().unwrap_or_else(|_| empty_paper_report(&id, true));
                    HttpResponse::json(200, json!({"result":result,"report":enrich_paper_report(report,&id)}))
                }
                Err(error) => HttpResponse::json(400, json!({"error":error.to_string()})),
            }
        } else if profile_simulator.is_some() {
            self.post_profile_paper_order(body, profile_simulator.unwrap())
        } else {
            let mut simulator = match self.global_paper.lock() { Ok(value) => value, Err(_) => return HttpResponse::json(503, json!({"error":"Paper ledger unavailable."})) };
            match action {
                "set-initial-balance" => {
                    let Some(amount) = body.get("amount").and_then(number_string) else { return HttpResponse::json(400,json!({"error":"Enter a valid virtual balance amount."})); };
                    let mint = body.get("mint").and_then(Value::as_str).unwrap_or("USD");
                    match simulator.set_initial_virtual_balance(&amount,mint) {
                        Ok(result) => HttpResponse::json(200,json!({"result":result,"report":simulator.report().unwrap_or(Value::Null)})),
                        Err(error) => HttpResponse::json(400,json!({"error":error.to_string()}))
                    }
                }
                "add-balance" => {
                    let mint = body.get("mint").and_then(Value::as_str).unwrap_or("");
                    let Some(amount) = body.get("amount").and_then(number_string) else { return HttpResponse::json(400,json!({"error":"Enter a valid virtual funding amount."})); };
                    match simulator.add_virtual_balance(mint,&amount) {
                        Ok(result) => HttpResponse::json(200,json!({"result":result,"report":simulator.report().unwrap_or(Value::Null)})),
                        Err(error) => HttpResponse::json(400,json!({"error":error.to_string()}))
                    }
                }
                "create-order" => self.create_manual_paper_order(&mut simulator, body),
                _ => HttpResponse::json(400,json!({"error":"Unknown paper-simulation action."}))
            }
        }
    }

    fn post_profile_paper_order(&self, body: &Value, (id, simulator): (String, Arc<Mutex<PaperSimulator>>)) -> HttpResponse {
        let mut simulator = match simulator.lock() { Ok(value) => value, Err(_) => return HttpResponse::json(503,json!({"error":"Paper ledger unavailable."})) };
        let action = body.get("action").and_then(Value::as_str).unwrap_or("");
        match action {
            "set-initial-balance" => {
                let Some(amount)=body.get("amount").and_then(number_string) else{return HttpResponse::json(400,json!({"error":"Enter a valid virtual balance amount."}));};
                match simulator.set_initial_virtual_balance(&amount,body.get("mint").and_then(Value::as_str).unwrap_or("USD")) {
                    Ok(result)=>HttpResponse::json(200,json!({"result":result,"report":enrich_paper_report(simulator.report().unwrap_or(Value::Null),&id)})),
                    Err(error)=>HttpResponse::json(400,json!({"error":error.to_string()}))
                }
            }
            "add-balance" => {
                let mint=body.get("mint").and_then(Value::as_str).unwrap_or("");
                let Some(amount)=body.get("amount").and_then(number_string) else{return HttpResponse::json(400,json!({"error":"Enter a valid virtual funding amount."}));};
                match simulator.add_virtual_balance(mint,&amount) {
                    Ok(result)=>HttpResponse::json(200,json!({"result":result,"report":enrich_paper_report(simulator.report().unwrap_or(Value::Null),&id)})),
                    Err(error)=>HttpResponse::json(400,json!({"error":error.to_string()}))
                }
            }
            "create-order" => self.create_manual_paper_order(&mut simulator,body),
            _ => HttpResponse::json(400,json!({"error":"Unknown paper-simulation action."}))
        }
    }

    fn create_manual_paper_order(&self, simulator: &mut PaperSimulator, body: &Value) -> HttpResponse {
        let Some(side)=body.get("side").and_then(Value::as_str) else{return HttpResponse::json(400,json!({"error":"Order side is required."}));};
        let Some(mint)=body.get("asset_mint").and_then(Value::as_str) else{return HttpResponse::json(400,json!({"error":"Asset mint is required."}));};
        let quote=body.get("quote_mint").and_then(Value::as_str).unwrap_or("USD");
        let Some(quantity)=body.get("quantity").and_then(number_string) else{return HttpResponse::json(400,json!({"error":"Order quantity is required."}));};
        let Some(price_raw)=body.get("price").and_then(number_string) else{return HttpResponse::json(400,json!({"error":"A manual paper reference price is required."}));};
        if mint.trim().is_empty() || mint.len()>128 || quote!="USD" || mint==quote {
            return HttpResponse::json(400,json!({"error":"A valid asset and USD quote currency are required."}));
        }
        let now=Utc::now().to_rfc3339_opts(SecondsFormat::Millis,true);
        let order=match simulator.create_order(CreateOrderRequest{
            signal_id:body.get("signal_id").and_then(Value::as_str).unwrap_or("manual-paper-order"),
            side,
            asset_mint:mint,
            quantity:&quantity,
            quote_mint:quote,
            created_at:&now,
            details:None,
        }) {
            Ok(value)=>value,
            Err(error)=>return HttpResponse::json(400,json!({"error":error.to_string()}))
        };
        if order.get("status").and_then(Value::as_str)==Some("DUPLICATE") {
            return HttpResponse::json(409,json!({"error":"This paper order signal was already processed."}));
        }
        let Some(order_id)=order.get("order_id").and_then(Value::as_str) else{return HttpResponse::json(500,json!({"error":"Paper order was not created."}));};
        let liquidity=body.get("liquidity_usd").and_then(number_string);
        let market=match MarketPrice::new(
            mint,&price_raw,"USD","user-entered-unverified",&now,
            liquidity.as_deref(),
            liquidity.as_ref().map(|_|"user-entered-unverified".to_string()),
            liquidity.as_ref().map(|_|now.clone())
        ) {
            Ok(value)=>value,
            Err(error)=>return HttpResponse::json(400,json!({"error":error.to_string()}))
        };
        let mut policy=ExecutionPolicy::default();
        policy.fee_bps=self.context.execution_policy.fee_bps;
        policy.network_cost=self.context.execution_policy.network_cost.clone();
        policy.priority_fee_usd=self.context.execution_policy.priority_fee_usd.clone();
        let result=match simulator.execute_order(order_id,Some(&market),&now,&policy,true) {
            Ok(value)=>value,
            Err(error)=>return HttpResponse::json(400,json!({"error":error.to_string()}))
        };
        HttpResponse::json(200,json!({"result":result,"report":simulator.report().unwrap_or(Value::Null)}))
    }
}

fn empty_paper_report(profile_id: &str, initialized: bool) -> Value {
    json!({
        "mode":"PAPER_SIMULATION","simulated":true,"real_wallet_balances_included":false,
        "real_trading":false,"transaction_broadcast":false,"profile_id":if profile_id.is_empty(){Value::Null}else{json!(profile_id)},
        "database_available":initialized,"balances":{},"orders":[],"fills":[],
        "available_balance":null,"available_balance_usd":null,"initial_balance":null,
        "cash_balance_usd":null,"token_holdings_value_usd":null,"token_holdings":[],
        "portfolio_value_usd":null,"total_portfolio_value_usd":null,
        "portfolio_value_status":"not_initialized","realized_pnl_usd":null,"unrealized_pnl_usd":null,
        "realized_roi_pct":null,"unrealized_roi_pct":null
    })
}

fn enrich_paper_report(mut report: Value, profile_id: &str) -> Value {
    if !report.is_object() { report = json!({}); }
    report["mode"]=json!("PAPER_SIMULATION");
    report["simulated"]=json!(true);
    report["real_wallet_balances_included"]=json!(false);
    report["real_trading"]=json!(false);
    report["transaction_broadcast"]=json!(false);
    if !profile_id.is_empty() { report["profile_id"]=json!(profile_id); }
    report["database_available"]=json!(true);
    report
}

fn canonical_profile_id(raw: &str) -> Result<String, String> {
    Uuid::parse_str(raw.trim()).map(|id|id.to_string()).map_err(|_|"A valid profile_id is required.".to_string())
}

fn number_string(value: &Value) -> Option<String> {
    value.as_str().map(str::to_string).or_else(|| value.as_number().map(ToString::to_string))
}

fn parse_positive(raw: Option<&String>, default: usize, max: usize) -> Result<usize,String> {
    let value=match raw {Some(raw)=>raw.parse::<usize>().map_err(|_|"Pagination value must be an integer.".to_string())?,None=>default};
    if value<1 || value>max {return Err("Pagination value is outside the allowed range.".to_string());}
    Ok(value)
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len()!=right.len() {return false;}
    let mut difference=0u8;
    for (a,b) in left.iter().zip(right) { difference |= a^b; }
    difference==0
}

fn status_reason(status: u16) -> &'static str {
    match status {
        200=>"OK",201=>"Created",400=>"Bad Request",401=>"Unauthorized",403=>"Forbidden",
        404=>"Not Found",405=>"Method Not Allowed",409=>"Conflict",413=>"Payload Too Large",
        429=>"Too Many Requests",500=>"Internal Server Error",503=>"Service Unavailable",_=>"Error"
    }
}

fn read_request(stream: &TcpStream, peer_ip: IpAddr) -> std::io::Result<HttpRequest> {
    let mut reader=BufReader::new(stream.try_clone()?);
    let mut request_line=String::new();
    reader.read_line(&mut request_line)?;
    let mut parts=request_line.split_whitespace();
    let method=parts.next().unwrap_or("").to_string();
    let target=parts.next().unwrap_or("/").to_string();
    let version=parts.next().unwrap_or("");
    if !matches!(version,"HTTP/1.0"|"HTTP/1.1") || method.is_empty() {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData,"Invalid HTTP request line"));
    }
    let mut headers=HashMap::new();
    let mut header_bytes=0usize;
    loop {
        let mut line=String::new();
        let read=reader.read_line(&mut line)?;
        if read==0 {break;}
        header_bytes+=read;
        if header_bytes>MAX_HEADERS_BYTES {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData,"HTTP headers too large"));
        }
        if line=="\r\n"||line=="\n" {break;}
        if let Some((name,value))=line.split_once(':') {
            headers.insert(name.trim().to_ascii_lowercase(),value.trim().to_string());
        }
    }
    if headers.get("transfer-encoding").is_some_and(|value|!value.eq_ignore_ascii_case("identity")) {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData,"Transfer encoding is unsupported"));
    }
    let length=headers.get("content-length").map(|value|value.parse::<usize>())
        .transpose().map_err(|_|std::io::Error::new(std::io::ErrorKind::InvalidData,"Invalid content length"))?.unwrap_or(0);
    if length>MAX_BODY_BYTES {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData,"Request body too large"));
    }
    let mut body=vec![0u8;length];
    reader.read_exact(&mut body)?;
    Ok(HttpRequest{method,target,headers,body,peer_ip})
}

fn write_response(mut stream: TcpStream, response: HttpResponse) -> std::io::Result<()> {
    let headers=format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:\r\n\r\n",
        response.status,status_reason(response.status),response.content_type,response.body.len()
    );
    stream.write_all(headers.as_bytes())?;
    stream.write_all(&response.body)?;
    stream.flush()
}

/// Start the Rust-native production dashboard. No Python interpreter is invoked.
pub fn serve_dashboard() -> Result<(), Box<dyn Error>> {
    let app=DashboardApp::from_environment()?;
    app.restore_monitors();
    let address=format!("{}:{}",app.host,app.port);
    let listener=TcpListener::bind(&address)?;
    println!("Engen Rust dashboard listening on {}",address);
    println!("Read-only RPC; paper-only simulation; transaction signing and broadcast are unavailable.");
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream)=>{
                let peer_ip=stream.peer_addr().map(|address|address.ip()).unwrap_or(IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED));
                let app=Arc::clone(&app);
                thread::spawn(move||{
                    let _=stream.set_read_timeout(Some(Duration::from_secs(10)));
                    let _=stream.set_write_timeout(Some(Duration::from_secs(10)));
                    let response=match read_request(&stream,peer_ip) {
                        Ok(request)=>app.handle(request),
                        Err(_)=>HttpResponse::json(400,json!({"error":"Malformed or oversized HTTP request."}))
                    };
                    let _=write_response(stream,response);
                });
            }
            Err(error)=>eprintln!("Dashboard accept failed: {}",error),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_ids_must_be_canonicalizable_uuids() {
        let value="e3d0a5b0-3b84-4df4-a9a7-0b7c8dc0a101";
        assert_eq!(canonical_profile_id(value).unwrap(),value);
        assert!(canonical_profile_id("../data").is_err());
    }

    #[test]
    fn auth_comparison_accepts_only_exact_equal_length_values() {
        assert!(constant_time_eq(b"same-token",b"same-token"));
        assert!(!constant_time_eq(b"same-token",b"same-toke"));
        assert!(!constant_time_eq(b"same-token",b"same-token-extra"));
    }

    #[test]
    fn pagination_has_bounded_defaults_and_rejects_zero_or_oversized_values() {
        assert_eq!(parse_positive(None,25,100).unwrap(),25);
        assert_eq!(parse_positive(Some(&"100".to_string()),25,100).unwrap(),100);
        assert!(parse_positive(Some(&"0".to_string()),25,100).is_err());
        assert!(parse_positive(Some(&"101".to_string()),25,100).is_err());
    }

    #[test]
    fn paper_report_is_never_marked_as_real_trading() {
        let report=enrich_paper_report(json!({"balances":{}}),"profile-id");
        assert_eq!(report["real_trading"],false);
        assert_eq!(report["transaction_broadcast"],false);
        assert_eq!(report["simulated"],true);
    }
}
