import { useEffect, useState } from "react";
import { pmApi } from "../../api/modules/pm";

const POLL_MS = 60_000;

/**
 * How many decision cards are waiting on a human, for the sidebar badge.
 *
 * Polling rather than a websocket on purpose: a card appears at most every few
 * seconds and a stale badge for under a minute costs nothing, while a second
 * socket per tab costs the whole shell. Failures are swallowed — a badge is
 * never worth breaking navigation over, and PM may be disabled on the instance.
 */
export function usePmInbox(enabled: boolean): number {
  const [pending, setPending] = useState(0);

  useEffect(() => {
    if (!enabled) {
      setPending(0);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;

    const load = async () => {
      // A hidden tab has nobody reading the badge; skip the request and pick the
      // count up again on the next visible tick.
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        timer = window.setTimeout(load, POLL_MS);
        return;
      }
      try {
        const data = await pmApi.inbox();
        if (!cancelled) setPending(data.pending ?? 0);
      } catch {
        if (!cancelled) setPending(0);
      }
      if (!cancelled) timer = window.setTimeout(load, POLL_MS);
    };

    void load();

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled]);

  return pending;
}
