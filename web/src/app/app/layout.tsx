import { redirect } from "next/navigation";
import type { Route } from "next";
import { unstable_noStore as noStore } from "next/cache";
import { requireAuth } from "@/lib/auth/requireAuth";
import { ProjectsProvider } from "@/providers/ProjectsContext";
import { VoiceModeProvider } from "@/providers/VoiceModeProvider";
import AppSidebar from "@/sections/sidebar/AppSidebar";
import NaarniConnectGate from "@/refresh-components/modals/NaarniConnectGate";
import { RootLayout } from "@opal/layouts";
import AppChrome from "@/sections/app-chrome/AppChrome";

export interface LayoutProps {
  children: React.ReactNode;
}

export default async function Layout({ children }: LayoutProps) {
  noStore();

  // Only check authentication - data fetching is done client-side via SWR hooks
  const authResult = await requireAuth();

  if (authResult.redirect) {
    redirect(authResult.redirect as Route);
  }

  return (
    <ProjectsProvider>
      {/* VoiceModeProvider wraps the full app layout so TTS playback state
          persists across page navigations (e.g., sidebar clicks during playback).
          It only activates WebSocket connections when TTS is actually triggered. */}
      <VoiceModeProvider>
        <RootLayout.Root>
          <AppSidebar />
          <AppChrome>{children}</AppChrome>
        </RootLayout.Root>
        {/* Post-OIDC Naarni Connect prompt. Self-gated — shows a modal
            asking the user to link their Naarni phone+OTP account, only
            if they haven't already linked and haven't dismissed it in
            the last 24h. Unmounts itself once they're connected. */}
        <NaarniConnectGate />
      </VoiceModeProvider>
    </ProjectsProvider>
  );
}
