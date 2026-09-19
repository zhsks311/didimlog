import { spawn } from "node:child_process";

const DIDIM = __DIDIM_LAUNCHER_JSON__;
const ROOT = __DIDIM_ROOT_JSON__;
const REVISION = __REVISION_JSON__;
const WARNING = "Didimlog 시작 상태를 확인하지 못했습니다. didim status를 실행하세요.";
const OUTPUT_LIMIT = 1024;

export default function didimlogExtension(pi) {
  let generation = 0;
  let scheduledTimer = null;
  let deadlineTimer = null;
  let timerContext = null;
  let child = null;
  let stopped = false;

  function killTree(running) {
    if (running === null) return;
    try {
      globalThis.process.kill(-running.pid, "SIGKILL");
    } catch {
      try {
        running.kill("SIGKILL");
      } catch {
        // The process has already exited.
      }
    }
    running.stdout?.destroy();
    running.stderr?.destroy();
  }

  function cancelCurrent() {
    generation += 1;
    if (timerContext !== null) {
      if (scheduledTimer !== null) timerContext.clearTimer(scheduledTimer);
      if (deadlineTimer !== null) timerContext.clearTimer(deadlineTimer);
    }
    scheduledTimer = null;
    deadlineTimer = null;
    timerContext = null;
    if (child !== null) {
      killTree(child);
      child = null;
    }
  }

  pi.on("session_start", (_event, ctx) => {
    cancelCurrent();
    stopped = false;
    const current = generation;
    timerContext = ctx;
    scheduledTimer = ctx.setTimeout(() => {
      scheduledTimer = null;
      if (stopped || current !== generation) return;

      let stdout = Buffer.alloc(0);
      let failed = false;
      let warned = false;
      const process = spawn(
        DIDIM,
        [
          "hook",
          "startup-check",
          "--client",
          "omp",
          "--root",
          ROOT,
          "--revision",
          REVISION,
          "--cwd",
          ctx.cwd,
        ],
        {
          cwd: ctx.cwd,
          env: {
            ...globalThis.process.env,
            PYTHONDONTWRITEBYTECODE: "1",
            DIDIM_NO_UPDATE_CHECK: "1",
          },
          stdio: ["ignore", "pipe", "pipe"],
          detached: true,
        },
      );
      child = process;

      const warn = () => {
        if (
          warned ||
          stopped ||
          current !== generation
        ) return;
        warned = true;
        ctx.ui.notify(WARNING, "warning");
      };
      const fail = () => {
        failed = true;
        killTree(process);
        warn();
      };
      const consume = (chunk, capture) => {
        if (failed) return;
        const nextLength = (capture ? stdout.length : 0) + chunk.length;
        if (nextLength > OUTPUT_LIMIT) {
          fail();
          return;
        }
        if (capture) stdout = Buffer.concat([stdout, chunk], nextLength);
      };
      process.stdout.on("data", (chunk) => consume(chunk, true));
      process.stderr.on("data", () => {
        fail();
      });
      process.on("error", () => {
        failed = true;
        warn();
      });
      process.on("close", (code, signal) => {
        if (timerContext !== null && deadlineTimer !== null) {
          timerContext.clearTimer(deadlineTimer);
        }
        deadlineTimer = null;
        if (child === process) child = null;
        const text = stdout.toString("utf8").trim();
        if (
          failed ||
          code !== 0 ||
          signal !== null ||
          (text !== "" && text !== "DIDIMLOG_STARTUP_WARNING")
        ) {
          warn();
        } else if (text === "DIDIMLOG_STARTUP_WARNING") {
          warn();
        }
      });
      deadlineTimer = ctx.setTimeout(() => {
        fail();
      }, 1900);
    }, 0);
  });

  pi.on("session_switch", () => {
    cancelCurrent();
  });
  pi.on("session_shutdown", () => {
    stopped = true;
    cancelCurrent();
  });
}
