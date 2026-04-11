"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Content } from "@opal/layouts";
import { Button } from "@opal/components";
import { SvgPlug, SvgUnplug, SvgCheck, SvgArrowLeft } from "@opal/icons";
import Card from "@/refresh-components/cards/Card";
import InputTypeIn from "@/refresh-components/inputs/InputTypeIn";
import OtpInput from "@/refresh-components/inputs/OtpInput";
import Text from "@/refresh-components/texts/Text";
import { Section } from "@/layouts/general-layouts";
import { toast } from "@/hooks/useToast";
import useNaarniAccount from "@/hooks/useNaarniAccount";
import { Disabled } from "@opal/core";

// ─── Flow state ──────────────────────────────────────────────────────────────
type Step = "idle" | "phone" | "otp" | "loading";

// Keep in sync with the backend normalize_phone_number helper. We validate in
// the UI so users get instant feedback instead of waiting for a round-trip.
// Naarni SMS OTPs are 4 digits (verified against Postman collection
// responses in /api/v1/auth/token: otp=9595, otp=4737, otp=1990).
const OTP_LENGTH = 4;
const RESEND_COOLDOWN_SECONDS = 30;

/**
 * Strip any non-digits and the optional +91 prefix from user input. Returns
 * a 10-digit string, or null if the input isn't a valid 10-digit phone.
 *
 * This mirrors the exact normalization done by the backend in
 * `token_refresh.normalize_phone_number`, so a value that passes here will
 * always be accepted by the API.
 */
function normalizePhone(raw: string): string | null {
  const noPrefix = raw.trim().replace(/^\+?91/, "");
  const digits = noPrefix.replace(/\D/g, "");
  return digits.length === 10 ? digits : null;
}

export default function NaarniConnectPage() {
  const router = useRouter();
  const { isConnected, phoneNumber, refetch } = useNaarniAccount();

  const [step, setStep] = useState<Step>("idle");
  const [phone, setPhone] = useState("");
  const [otp, setOtp] = useState("");
  const [error, setError] = useState("");
  const [isDisconnecting, setIsDisconnecting] = useState(false);

  // Resend countdown — counts seconds until the user can hit Resend again.
  const [resendSeconds, setResendSeconds] = useState(0);
  // Track the countdown interval so we can clear it on unmount.
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Start a fresh 30-second countdown after each successful OTP request.
  const startResendCountdown = useCallback(() => {
    setResendSeconds(RESEND_COOLDOWN_SECONDS);
    if (countdownRef.current) clearInterval(countdownRef.current);
    countdownRef.current = setInterval(() => {
      setResendSeconds((prev) => {
        if (prev <= 1) {
          if (countdownRef.current) {
            clearInterval(countdownRef.current);
            countdownRef.current = null;
          }
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
  }, []);

  // Clear the interval on unmount to avoid setState-on-unmounted warnings.
  useEffect(() => {
    return () => {
      if (countdownRef.current) clearInterval(countdownRef.current);
    };
  }, []);

  // Memoized validation so the UI can disable buttons when input is bad.
  const normalizedPhone = useMemo(() => normalizePhone(phone), [phone]);
  const canSendOtp = normalizedPhone !== null && step !== "loading";
  const canVerifyOtp = otp.length === OTP_LENGTH && step !== "loading";

  // ── Step 1: request OTP ───────────────────────────────────────────────
  const handleRequestOtp = useCallback(
    async (isResend: boolean = false) => {
      if (!normalizedPhone) {
        setError("Please enter a valid 10-digit phone number.");
        return;
      }
      setError("");
      setStep("loading");
      try {
        const resp = await fetch("/api/naarni-auth/request-otp", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone_number: normalizedPhone }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(
            body.detail || "Failed to send OTP. Please try again."
          );
        }
        toast.success(
          isResend
            ? "New OTP sent — check your SMS."
            : "OTP sent! Check your SMS."
        );
        // Reset the OTP input whenever a fresh code is sent.
        setOtp("");
        setStep("otp");
        startResendCountdown();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to send OTP");
        // Stay on the phone step on first attempt; stay on otp step on resend
        // so the user doesn't lose their place.
        setStep(isResend ? "otp" : "phone");
      }
    },
    [normalizedPhone, startResendCountdown]
  );

  // ── Step 2: verify OTP ────────────────────────────────────────────────
  const handleVerifyOtp = useCallback(
    async (codeOverride?: string) => {
      const code = (codeOverride ?? otp).trim();
      if (code.length !== OTP_LENGTH) {
        setError(`Please enter the ${OTP_LENGTH}-digit OTP.`);
        return;
      }
      if (!normalizedPhone) {
        setError("Phone number is invalid — please re-enter it.");
        setStep("phone");
        return;
      }
      setError("");
      setStep("loading");
      try {
        const resp = await fetch("/api/naarni-auth/verify-otp", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone_number: normalizedPhone, otp: code }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(
            body.detail || "Invalid OTP. Please check and try again."
          );
        }
        toast.success("Naarni account connected successfully!");
        setPhone("");
        setOtp("");
        setStep("idle");
        refetch();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Verification failed");
        setStep("otp");
      }
    },
    [normalizedPhone, otp, refetch]
  );

  // ── Disconnect ────────────────────────────────────────────────────────
  const handleDisconnect = useCallback(async () => {
    setIsDisconnecting(true);
    try {
      const resp = await fetch("/api/naarni-auth/disconnect", {
        method: "POST",
      });
      if (resp.ok) {
        toast.success("Naarni account disconnected.");
        refetch();
      } else {
        throw new Error("Failed to disconnect");
      }
    } catch {
      toast.error("Failed to disconnect.");
    } finally {
      setIsDisconnecting(false);
    }
  }, [refetch]);

  // ── Connected state ───────────────────────────────────────────────────
  if (isConnected) {
    return (
      <Section gap={1.5} className="w-full">
        <Content
          icon={SvgPlug}
          title="Naarni Fleet Account"
          description="Your fleet account is linked. You can now chat with your vehicle data."
          sizePreset="main-content"
          variant="section"
        />

        <Card padding={1}>
          <Section gap={1} alignItems="start">
            <Section flexDirection="row" gap={0.5} alignItems="center">
              <SvgCheck className="text-status-success-text-01 w-5 h-5" />
              <Text>
                Connected as <Text className="!font-bold">{phoneNumber}</Text>
              </Text>
            </Section>

            <Text text03>
              Ask questions like &quot;How many vehicles do we have?&quot; or
              &quot;Show me this week&apos;s fleet performance&quot; in the
              chat.
            </Text>

            <Section flexDirection="row" gap={0.5}>
              <Button
                prominence="secondary"
                onClick={() => router.push("/app")}
              >
                Go to Chat
              </Button>
              <Disabled disabled={isDisconnecting}>
                <Button
                  variant="danger"
                  prominence="tertiary"
                  icon={SvgUnplug}
                  onClick={() => void handleDisconnect()}
                >
                  {isDisconnecting ? "Disconnecting..." : "Disconnect"}
                </Button>
              </Disabled>
            </Section>
          </Section>
        </Card>
      </Section>
    );
  }

  // ── Not connected — show the 2-step flow ──────────────────────────────
  return (
    <Section gap={1.5} className="w-full">
      <Content
        icon={SvgPlug}
        title="Connect Naarni Fleet"
        description="Link your Naarni account to chat with live vehicle data — routes, performance, alerts, and more."
        sizePreset="main-content"
        variant="section"
      />

      <Card padding={1.5}>
        <Section gap={1} alignItems="start">
          {/* Step indicator — highlights current step */}
          <Section flexDirection="row" gap={0.75} alignItems="center">
            <div
              className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${
                step === "phone" || step === "idle"
                  ? "bg-theme-primary-05 text-text-inverted-01"
                  : "bg-background-neutral-03 text-text-03"
              }`}
            >
              1
            </div>
            <div className="w-8 h-px bg-border-02" />
            <div
              className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${
                step === "otp"
                  ? "bg-theme-primary-05 text-text-inverted-01"
                  : "bg-background-neutral-03 text-text-03"
              }`}
            >
              2
            </div>
          </Section>

          {error && <Text className="text-status-error-text-01">{error}</Text>}

          {/* Step 1: Phone Number */}
          {(step === "idle" || step === "phone") && (
            <Section gap={0.75} className="w-full">
              <Text text02 className="!font-medium">
                Enter your Naarni phone number
              </Text>
              <Text text03>
                We&apos;ll send a 4-digit code to this number via SMS. Enter the
                10-digit number without the country code.
              </Text>
              <InputTypeIn
                placeholder="Phone number (10 digits, e.g. 9999955555)"
                value={phone}
                onChange={(e) => {
                  setPhone(e.target.value);
                  if (error) setError("");
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && canSendOtp) {
                    void handleRequestOtp(false);
                  }
                }}
              />
              <Section flexDirection="row" gap={0.5}>
                <Disabled disabled={!canSendOtp}>
                  <Button
                    prominence="primary"
                    onClick={() => void handleRequestOtp(false)}
                  >
                    Send OTP
                  </Button>
                </Disabled>
              </Section>
            </Section>
          )}

          {/* Step 2: OTP Verification */}
          {step === "otp" && (
            <Section gap={0.75} className="w-full">
              <Text text02 className="!font-medium">
                Enter the OTP
              </Text>
              <Text text03>
                A {OTP_LENGTH}-digit code was sent to{" "}
                <Text className="!font-bold">{normalizedPhone}</Text>.
              </Text>
              <OtpInput
                value={otp}
                onChange={(val) => {
                  setOtp(val);
                  if (error) setError("");
                }}
                onComplete={(val) => void handleVerifyOtp(val)}
                length={OTP_LENGTH}
                error={!!error}
                autoFocus
              />

              {/* Resend — disabled until the 30s cooldown finishes */}
              <Section flexDirection="row" gap={0.5} alignItems="center">
                <Text text03>
                  {resendSeconds > 0
                    ? `Resend available in ${resendSeconds}s`
                    : "Didn't get it?"}
                </Text>
                {/* Note: step is already narrowed to "otp" in this branch,
                    so the only thing that can disable resend here is the
                    30-second cooldown. */}
                <Disabled disabled={resendSeconds > 0}>
                  <Button
                    prominence="tertiary"
                    onClick={() => void handleRequestOtp(true)}
                  >
                    Resend OTP
                  </Button>
                </Disabled>
              </Section>

              <Section flexDirection="row" gap={0.5}>
                <Button
                  prominence="secondary"
                  icon={SvgArrowLeft}
                  onClick={() => {
                    setStep("phone");
                    setOtp("");
                    setError("");
                    if (countdownRef.current) {
                      clearInterval(countdownRef.current);
                      countdownRef.current = null;
                    }
                    setResendSeconds(0);
                  }}
                >
                  Back
                </Button>
                <Disabled disabled={!canVerifyOtp}>
                  <Button
                    prominence="primary"
                    onClick={() => void handleVerifyOtp()}
                  >
                    Verify & Connect
                  </Button>
                </Disabled>
              </Section>
            </Section>
          )}

          {/* Step: Loading */}
          {step === "loading" && <Text text03>Connecting to Naarni...</Text>}
        </Section>
      </Card>
    </Section>
  );
}
