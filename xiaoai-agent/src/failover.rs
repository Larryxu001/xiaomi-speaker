//! Ordered fallback between providers (ASR services, chat models).
//!
//! Each turn tries providers starting from the one that worked last, so a dead
//! primary (no balance, bad key, outage) doesn't cost a timeout every turn.
//! After `RETRY_PRIMARY_AFTER` the primary gets another chance.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const RETRY_PRIMARY_AFTER: Duration = Duration::from_secs(600);

pub struct Failover {
    preferred: AtomicUsize,
    since: Mutex<Instant>,
}

impl Default for Failover {
    fn default() -> Self {
        Self {
            preferred: AtomicUsize::new(0),
            since: Mutex::new(Instant::now()),
        }
    }
}

impl Failover {
    /// Indices to try this turn, preferred one first.
    pub fn order(&self, len: usize) -> Vec<usize> {
        let mut start = self.preferred.load(Ordering::Relaxed).min(len.saturating_sub(1));
        if start != 0 && self.since.lock().map(|t| t.elapsed() > RETRY_PRIMARY_AFTER).unwrap_or(true) {
            start = 0;
        }
        (0..len).map(|i| (start + i) % len).collect()
    }

    pub fn succeeded(&self, index: usize) {
        if self.preferred.swap(index, Ordering::Relaxed) != index {
            if let Ok(mut t) = self.since.lock() {
                *t = Instant::now();
            }
        }
    }
}

/// Errors that retrying the same provider won't fix: no balance, bad key,
/// wrong model, rate limit. Switch provider immediately on these.
pub fn is_hard_failure(err: &anyhow::Error) -> bool {
    let text = format!("{err:#}");
    ["status=400", "status=401", "status=402", "status=403", "status=404", "status=429",
     "insufficient", "balance", "Unauthorized", "401", "402", "403", "429"]
        .iter()
        .any(|needle| text.contains(needle))
}
