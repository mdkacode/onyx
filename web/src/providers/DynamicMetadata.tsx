"use client";

import { useEffect, useMemo } from "react";
import { useSettingsContext } from "@/providers/SettingsProvider";

export default function DynamicMetadata() {
  const { enterpriseSettings } = useSettingsContext();

  useEffect(() => {
    const title = enterpriseSettings?.application_name || "Naarni";
    if (document.title !== title) {
      document.title = title;
    }
  }, [enterpriseSettings]);

  // Cache-buster so the favicon re-fetches after an admin uploads a new logo.
  const cacheBuster = useMemo(
    () => Date.now(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [enterpriseSettings]
  );

  if (enterpriseSettings?.use_custom_logo) {
    return (
      <link
        rel="icon"
        href={`/api/enterprise-settings/logo?v=${cacheBuster}`}
      />
    );
  }

  // `icon.svg` recolors itself with the browser's color scheme, so the NaArNi
  // mark stays visible on both light and dark tab bars. Browsers that ignore
  // SVG favicons (Safari) fall back to the `.ico`.
  return (
    <>
      <link rel="icon" type="image/svg+xml" href="/icon.svg" />
      <link rel="alternate icon" type="image/x-icon" href="/favicon.ico" />
    </>
  );
}
