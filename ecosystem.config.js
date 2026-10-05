// pm2 start ecosystem.config.js && pm2 save
// Absolute paths on purpose (PM2 + WSL resolves relative cwd/script unreliably).
// Uses the project's own venv so the bot never depends on whatever venv a shell had active.
module.exports = {
  apps: [
    {
      name: "qubodesk-solver",
      cwd: "/mnt/c/Github/qubo-desk",
      script: "/mnt/c/Github/qubo-desk/.venv/bin/python",
      args: "-m qubodesk.bot --state /mnt/c/Github/qubo-desk/solver-state.json --max-solve 20 --poll 5",
      interpreter: "none",
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 50,
    },
  ],
};
