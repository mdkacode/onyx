"use client";

import { useEffect, useState } from "react";
import useNaarniAccount from "@/hooks/useNaarniAccount";
import NaarniConnectModal from "@/refresh-components/modals/NaarniConnectModal";

/**
 * NaarniConnectGate — mount-once component that decides whether to show
 * the post-OIDC Naarni Connect modal to the current user.
 *
 * Shows the modal when ALL of the following are true:
 *   1. useNaarniAccount finished loading (`isLoading === false`)
 *   2. The user has NOT linked their Naarni account (`isConnected === false`)
 *   3. The user has NOT dismissed the modal this session
 *      (localStorage key `naarni-connect-dismissed-at` is null or older than
 *      24 hours — a "snooze", not a permanent dismissal)
 *
 * Design notes:
 *   - This modal appears AFTER the user has already completed Microsoft
 *     OIDC login. It does NOT replace OIDC. It's a secondary prompt to
 *     link their Naarni fleet account so chat tools (Fleet Data,
 *     Naarni Dashboard) can query fleet data on their behalf.
 *   - "Skip for now" sets the dismissed-at timestamp. The modal will NOT
 *     reappear for 24 hours, even across page reloads. After 24 hours it
 *     reappears once per day until the user links.
 *   - Successful link calls `refetch()` which flips `isConnected` to
 *     true, which unmounts the modal naturally.
 *   - Mount this component once near the top of the authenticated app
 *     layout. It owns the modal lifecycle entirely.
 */

const DISMISSAL_STORAGE_KEY = "naarni-connect-dismissed-at";
const DISMISSAL_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

function isDismissedRecently(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = window.localStorage.getItem(DISMISSAL_STORAGE_KEY);
    if (!raw) return false;
    const ts = parseInt(raw, 10);
    if (Number.isNaN(ts)) return false;
    return Date.now() - ts < DISMISSAL_TTL_MS;
  } catch {
    // localStorage may throw in private mode / SSR — treat as not dismissed
    return false;
  }
}

function markDismissed(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DISMISSAL_STORAGE_KEY, String(Date.now()));
  } catch {
    // ignore — dismissal just doesn't persist, modal will reappear next load
  }
}

function clearDismissal(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(DISMISSAL_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export default function NaarniConnectGate() {
  const { isConnected, isLoading, refetch } = useNaarniAccount();

  // `dismissed` reflects the localStorage state. We load it once on mount
  // and then update it locally when the user clicks Skip — this avoids
  // re-reading localStorage on every render.
  const [dismissed, setDismissed] = useState<boolean>(true);

  // Initialize `dismissed` from localStorage after hydration to avoid
  // server/client hydration mismatches. If the user has dismissed the
  // modal within the last 24h, we stay quiet. Otherwise we let the
  // useNaarniAccount state decide whether to show it.
  useEffect(() => {
    setDismissed(isDismissedRecently());
  }, []);

  // Don't render anything while we're still fetching the account status —
  // otherwise the modal would flash open for a moment before hiding.
  if (isLoading) return null;

  // Already connected → unmount any previous dismissal state so the modal
  // will reappear next time the user disconnects, and render nothing.
  if (isConnected) {
    return null;
  }

  // User has not connected and has not recently dismissed → show the modal.
  if (dismissed) return null;

  return (
    <NaarniConnectModal
      onSuccess={() => {
        // Clear any old dismissal timestamp so re-disconnecting later
        // triggers the modal on the next session.
        clearDismissal();
        // Refetch the account status — this flips isConnected to true
        // and causes this component to return null above.
        refetch();
      }}
      onSkip={() => {
        markDismissed();
        setDismissed(true);
      }}
    />
  );
}
