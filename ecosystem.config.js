// pm2 start ecosystem.config.js --only qubodesk-solver-1,qubodesk-solver-2 && pm2 save
// Absolute paths on purpose (PM2 + WSL resolves relative cwd/script unreliably).
// Each solver has its own key file (.env.solverN) and its own state file; shared settings come from .env.
const ROOT = "/mnt/c/Github/qubo-desk";
const PY = `${ROOT}/.venv/bin/python`;

const solver = (name, args) => ({
  name,
  cwd: ROOT,
  script: PY,
  args,
  interpreter: "none",
  autorestart: true,
  restart_delay: 10000,
  max_restarts: 50,
});

module.exports = {
  apps: [
    // original single solver — runs with the CLIENT key from .env (job 0). Keep stopped for multi-wallet jobs.
    solver("qubodesk-solver", `-m qubodesk.bot --state ${ROOT}/solver-state.json --max-solve 20 --poll 5`),
    // independent solvers: different wallets, different compute budgets
    solver("qubodesk-solver-1",
      `-m qubodesk.bot --env-file ${ROOT}/.env.solver1 --state ${ROOT}/solver1-state.json --max-solve 25 --poll 5`),
    solver("qubodesk-solver-2",
      `-m qubodesk.bot --env-file ${ROOT}/.env.solver2 --state ${ROOT}/solver2-state.json --max-solve 6 --poll 5`),
  ],
};
