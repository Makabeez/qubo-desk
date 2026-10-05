//! qsa — multi-threaded simulated annealing for QUBO, zero dependencies.
//!
//! Input (file or stdin):
//!     n m
//!     i j w        (m lines, 0-indexed, w may be float; i == j is a linear term)
//! Minimises E(x) = Σ w·x_i·x_j.
//!
//! Output: one JSON line {"x":"0x…","energy":…,"restarts":…,"sweeps":…,"ms":…}
//! where bit i of x is x_i.

use std::io::{self, Read};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

struct Problem {
    n: usize,
    diag: Vec<f64>,
    adj: Vec<Vec<(usize, f64)>>,
    beta_hot: f64,
    beta_cold: f64,
}

struct Opts {
    path: Option<String>,
    sweeps: usize,
    restarts: u64, // 0 = until time budget
    threads: usize,
    seconds: f64,
    seed: u64,
    init: Option<Vec<u8>>,
}

// xorshift64* — fast, good enough for Metropolis acceptance
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }
    fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }
}

fn parse_args() -> Opts {
    let mut o = Opts { path: None, sweeps: 1000, restarts: 64, threads: 0, seconds: 0.0, seed: 1, init: None };
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut k = 0;
    while k < args.len() {
        let a = args[k].as_str();
        let mut val = || {
            k += 1;
            args.get(k).cloned().unwrap_or_else(|| die(&format!("missing value for {a}")))
        };
        match a {
            "--sweeps" => o.sweeps = val().parse().unwrap_or_else(|_| die("bad --sweeps")),
            "--restarts" => o.restarts = val().parse().unwrap_or_else(|_| die("bad --restarts")),
            "--threads" => o.threads = val().parse().unwrap_or_else(|_| die("bad --threads")),
            "--seconds" => o.seconds = val().parse().unwrap_or_else(|_| die("bad --seconds")),
            "--seed" => o.seed = val().parse().unwrap_or_else(|_| die("bad --seed")),
            "--init" => o.init = Some(hex_to_bits(&val())),
            "-h" | "--help" => {
                eprintln!("usage: qsa [FILE] [--sweeps N] [--restarts N|0] [--threads N] [--seconds S] [--seed N] [--init 0xHEX]");
                std::process::exit(0);
            }
            p if !p.starts_with("--") => o.path = Some(p.to_string()),
            _ => die(&format!("unknown flag {a}")),
        }
        k += 1;
    }
    if o.threads == 0 {
        o.threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    }
    if o.restarts == 0 && o.seconds <= 0.0 {
        die("--restarts 0 requires --seconds");
    }
    o
}

fn die(msg: &str) -> ! {
    eprintln!("qsa: {msg}");
    std::process::exit(2)
}

fn load(path: &Option<String>) -> Problem {
    let mut s = String::new();
    match path {
        Some(p) => s = std::fs::read_to_string(p).unwrap_or_else(|e| die(&format!("{p}: {e}"))),
        None => {
            io::stdin().read_to_string(&mut s).unwrap();
        }
    }
    let mut it = s.split_ascii_whitespace();
    let mut next = || it.next().unwrap_or_else(|| die("truncated input"));
    let n: usize = next().parse().unwrap_or_else(|_| die("bad n"));
    let m: usize = next().parse().unwrap_or_else(|_| die("bad m"));
    let mut diag = vec![0.0; n];
    let mut adj = vec![Vec::new(); n];
    for _ in 0..m {
        let i: usize = next().parse().unwrap_or_else(|_| die("bad i"));
        let j: usize = next().parse().unwrap_or_else(|_| die("bad j"));
        let w: f64 = next().parse().unwrap_or_else(|_| die("bad w"));
        if i >= n || j >= n {
            die("index out of range");
        }
        if i == j {
            diag[i] += w;
        } else {
            adj[i].push((j, w));
            adj[j].push((i, w));
        }
    }
    // Temperature range (same heuristic as dwave-neal): hottest move accepted with p=0.5,
    // smallest move accepted with p=0.01 at the cold end.
    let mut max_delta: f64 = 0.0;
    let mut min_delta = f64::INFINITY;
    for i in 0..n {
        let mut bound = diag[i].abs();
        if diag[i] != 0.0 {
            min_delta = min_delta.min(diag[i].abs());
        }
        for &(_, w) in &adj[i] {
            bound += w.abs();
            if w != 0.0 {
                min_delta = min_delta.min(w.abs());
            }
        }
        max_delta = max_delta.max(bound);
    }
    if max_delta == 0.0 {
        max_delta = 1.0;
        min_delta = 1.0;
    }
    let beta_hot = std::f64::consts::LN_2 / max_delta;
    let beta_cold = (100f64).ln() / min_delta;
    Problem { n, diag, adj, beta_hot, beta_cold }
}

fn energy(p: &Problem, x: &[u8]) -> f64 {
    let mut e = 0.0;
    for i in 0..p.n {
        if x[i] == 1 {
            e += p.diag[i];
            for &(j, w) in &p.adj[i] {
                if j > i && x[j] == 1 {
                    e += w;
                }
            }
        }
    }
    e
}

fn fields(p: &Problem, x: &[u8]) -> Vec<f64> {
    (0..p.n)
        .map(|i| p.diag[i] + p.adj[i].iter().map(|&(j, w)| if x[j] == 1 { w } else { 0.0 }).sum::<f64>())
        .collect()
}

#[inline]
fn flip(p: &Problem, x: &mut [u8], h: &mut [f64], i: usize) {
    let s = if x[i] == 0 { 1.0 } else { -1.0 };
    x[i] ^= 1;
    for &(j, w) in &p.adj[i] {
        h[j] += w * s;
    }
}

/// One annealing run followed by steepest-descent polish. Returns (x, energy).
fn anneal(p: &Problem, sweeps: usize, rng: &mut Rng, init: Option<&[u8]>) -> (Vec<u8>, f64) {
    let n = p.n;
    let mut x: Vec<u8> = match init {
        Some(v) => v.to_vec(),
        None => (0..n).map(|_| (rng.next() & 1) as u8).collect(),
    };
    let mut h = fields(p, &x);
    let mut e = energy(p, &x);
    let (mut best_x, mut best_e) = (x.clone(), e);
    let ratio = p.beta_cold / p.beta_hot;
    for s in 0..sweeps {
        let t = if sweeps > 1 { s as f64 / (sweeps - 1) as f64 } else { 1.0 };
        let beta = p.beta_hot * ratio.powf(t);
        for i in 0..n {
            let d = if x[i] == 0 { h[i] } else { -h[i] };
            if d <= 0.0 || rng.unit() < (-beta * d).exp() {
                flip(p, &mut x, &mut h, i);
                e += d;
            }
        }
        if e < best_e {
            best_e = e;
            best_x.copy_from_slice(&x);
        }
    }
    // polish the best state to a 1-flip local minimum
    x = best_x;
    h = fields(p, &x);
    e = best_e;
    loop {
        let mut improved = false;
        for i in 0..n {
            let d = if x[i] == 0 { h[i] } else { -h[i] };
            if d < -1e-12 {
                flip(p, &mut x, &mut h, i);
                e += d;
                improved = true;
            }
        }
        if !improved {
            break;
        }
    }
    let exact = energy(p, &x); // drop accumulated float error
    let _ = e;
    (x, exact)
}

fn hex_to_bits(s: &str) -> Vec<u8> {
    let s = s.trim_start_matches("0x");
    let mut bits = Vec::new();
    for c in s.chars().rev() {
        let v = c.to_digit(16).unwrap_or_else(|| die("bad hex in --init"));
        for b in 0..4 {
            bits.push(((v >> b) & 1) as u8);
        }
    }
    bits
}

fn bits_to_hex(x: &[u8]) -> String {
    let mut digits = Vec::new();
    for chunk in x.chunks(4) {
        let mut v = 0u32;
        for (b, &bit) in chunk.iter().enumerate() {
            v |= (bit as u32) << b;
        }
        digits.push(std::char::from_digit(v, 16).unwrap());
    }
    while digits.len() > 1 && *digits.last().unwrap() == '0' {
        digits.pop();
    }
    digits.reverse();
    format!("0x{}", digits.into_iter().collect::<String>())
}

fn main() {
    let o = parse_args();
    let p = load(&o.path);
    let init = o.init.as_ref().map(|b| {
        let mut v = b.clone();
        v.resize(p.n, 0);
        v
    });
    let start = Instant::now();
    let deadline = (o.seconds > 0.0).then(|| start + Duration::from_secs_f64(o.seconds));
    let done = AtomicU64::new(0);
    let completed = AtomicU64::new(0);
    let best: Mutex<(Vec<u8>, f64)> = Mutex::new((vec![0; p.n], energy(&p, &vec![0; p.n])));

    std::thread::scope(|sc| {
        for t in 0..o.threads {
            let (p, best, done, completed, init, o) = (&p, &best, &done, &completed, &init, &o);
            sc.spawn(move || {
                let mut rng = Rng(o.seed.wrapping_mul(0x9E3779B97F4A7C15).wrapping_add(t as u64 * 7919 + 1) | 1);
                loop {
                    let k = done.fetch_add(1, Ordering::Relaxed);
                    if o.restarts > 0 && k >= o.restarts {
                        break;
                    }
                    if deadline.map_or(false, |d| Instant::now() >= d) {
                        break;
                    }
                    // first run of thread 0 starts from the warm start, if any
                    let warm = if k == 0 { init.as_deref() } else { None };
                    let (x, e) = anneal(p, o.sweeps, &mut rng, warm);
                    completed.fetch_add(1, Ordering::Relaxed);
                    let mut b = best.lock().unwrap();
                    if e < b.1 {
                        *b = (x, e);
                    }
                }
            });
        }
    });

    let (x, e) = best.into_inner().unwrap();
    let restarts = completed.load(Ordering::Relaxed);
    println!(
        "{{\"x\":\"{}\",\"energy\":{},\"n\":{},\"restarts\":{},\"sweeps\":{},\"ms\":{}}}",
        bits_to_hex(&x),
        e,
        p.n,
        restarts,
        o.sweeps,
        start.elapsed().as_millis()
    );
}
