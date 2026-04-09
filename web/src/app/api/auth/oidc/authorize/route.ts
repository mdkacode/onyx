import { buildUrl } from "@/lib/utilsSS";
import { NextRequest, NextResponse } from "next/server";

export const GET = async (request: NextRequest) => {
  // Proxy the OIDC authorize request to the backend
  const url = new URL(buildUrl("/auth/oidc/authorize"));
  url.search = request.nextUrl.search;

  const response = await fetch(url.toString(), {
    redirect: "manual",
    headers: {
      cookie: request.headers.get("cookie") || "",
    },
  });

  // Get the redirect URL from backend (points to Microsoft login)
  let redirectUrl = response.headers.get("location") || "";

  // In dev mode, rewrite the redirect_uri from production domain to localhost
  if (process.env.NODE_ENV === "development" && redirectUrl) {
    redirectUrl = redirectUrl.replace(
      /redirect_uri=https?%3A%2F%2Fai\.naarni\.com/g,
      `redirect_uri=${encodeURIComponent("http://localhost:3000")}`
    );
    redirectUrl = redirectUrl.replace(
      /redirect_uri=https?:\/\/ai\.naarni\.com/g,
      "redirect_uri=http://localhost:3000"
    );
  }

  const redirectResponse = NextResponse.redirect(redirectUrl);

  // Forward any cookies from the backend
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) {
    redirectResponse.headers.set("set-cookie", setCookie);
  }

  return redirectResponse;
};
