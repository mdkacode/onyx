"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Modal from "@/refresh-components/Modal";
import { Section } from "@/layouts/general-layouts";
import InputTypeIn from "@/refresh-components/inputs/InputTypeIn";
import OtpInput from "@/refresh-components/inputs/OtpInput";
import Text from "@/refresh-components/texts/Text";
import { Button } from "@opal/components";
import { Disabled } from "@opal/core";
import { SvgArrowLeft, SvgPlug } from "@opal/icons";
import { toast } from "@/hooks/useToast";

/**
 * NaarniConnectModal — post-OIDC prompt that asks an already-logged-in
 * ONYX user to link their Naarni fleet account via phone + OTP.
 *
 * This modal reuses the *same* backend endpoints as the Naarni Connect
 * settings page:
 *   POST /api/naarni-auth/request-otp
 *   POST /api/naarni-auth/verify-otp
 *
 * The OTP input, phone normalization, 30-second resend cooldown, and
 * validation rules are all identical to the settings page — they share
 * the `OtpInput` component and the `normalizePhone` helper.
 *
 * Mounting + gating: see `NaarniConnectGate` (sibling file) which decides
 * whether to show this modal at all based on `useNaarniAccount()` + a
 * session-scoped localStorage dismissal flag.
 */

const OTP_LENGTH = 6;
const RESEND_COOLDOWN_SECONDS = 30;

type Step = "phone" | "otp" | "loading";

// Mirrors backend token_refresh.normalize_phone_number exactly.
function normalizePhone(raw: string): string | null {
  const noPrefix = raw.trim().replace(/^\+?91/, "");
  const digits = noPrefix.replace(/\D/g, "");
  return digits.length === 10 ? digits : null;
}

export interface NaarniConnectModalProps {
  /** Called when the user successfully links their Naarni account. */
  onSuccess: () => void;
  /** Called when the user clicks "Skip for now". */
  onSkip: () => void;
}

export default function NaarniConnectModal({
  onSuccess,
  onSkip,
}: NaarniConnectModalProps) {
  const [step, setStep] = useState<Step>("phone");
  const [phone, setPhone] = useState("");
  const [otp, setOtp] = useState("");
  const [error, setError] = useState("");

  // 30-second cooldown between OTP resends.
  const [resendSeconds, setResendSeconds] = useState(0);
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null);

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

  useEffect(() => {
    return () => {
      if (countdownRef.current) clearInterval(countdownRef.current);
    };
  }, []);

  const normalizedPhone = useMemo(() => normalizePhone(phone), [phone]);
  const canSendOtp = normalizedPhone !== null && step !== "loading";
  const canVerifyOtp = otp.length === OTP_LENGTH && step !== "loading";

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
        setOtp("");
        setStep("otp");
        startResendCountdown();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to send OTP");
        setStep(isResend ? "otp" : "phone");
      }
    },
    [normalizedPhone, startResendCountdown]
  );

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
          throw new Error(body.detail || "Invalid OTP. Please try again.");
        }
        toast.success("Naarni account linked successfully!");
        onSuccess();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Verification failed");
        setStep("otp");
      }
    },
    [normalizedPhone, otp, onSuccess]
  );

  // This modal is shown declaratively via `open`. If the user tries to
  // dismiss it via overlay click or Escape, we route that through
  // onSkip() so the parent can remember the dismissal.
  return (
    <Modal
      open
      onOpenChange={(open) => {
        if (!open) onSkip();
      }}
    >
      <Modal.Content width="sm" height="fit" preventAccidentalClose={false}>
        <Modal.Header
          icon={SvgPlug}
          title="Connect your Naarni fleet account"
          description="Link your phone number to chat with live vehicle data — routes, performance, alerts, and more."
          onClose={onSkip}
        />

        <Modal.Body padding={1.5}>
          <Section gap={1} alignItems="start">
            {/* Step indicator */}
            <Section flexDirection="row" gap={0.75} alignItems="center">
              <div
                className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${
                  step === "phone"
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

            {error && (
              <Text className="text-status-error-text-01">{error}</Text>
            )}

            {/* Step 1: phone number */}
            {step === "phone" && (
              <Section gap={0.75} className="w-full">
                <Text text02 className="!font-medium">
                  Enter your Naarni phone number
                </Text>
                <Text text03>
                  We&apos;ll send a 6-digit code via SMS. Enter the 10-digit
                  number without the country code.
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
                  <Button prominence="tertiary" onClick={onSkip}>
                    Skip for now
                  </Button>
                </Section>
              </Section>
            )}

            {/* Step 2: OTP verification */}
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

                {/* Resend cooldown */}
                <Section flexDirection="row" gap={0.5} alignItems="center">
                  <Text text03>
                    {resendSeconds > 0
                      ? `Resend available in ${resendSeconds}s`
                      : "Didn't get it?"}
                  </Text>
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

            {/* Loading */}
            {step === "loading" && <Text text03>Connecting to Naarni...</Text>}
          </Section>
        </Modal.Body>
      </Modal.Content>
    </Modal>
  );
}
