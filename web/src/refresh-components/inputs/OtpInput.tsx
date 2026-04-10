"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * OtpInput — a fixed-length, box-per-digit input for OTP / PIN codes.
 *
 * Behaviors:
 *   - Typing a digit auto-advances focus to the next box
 *   - Backspace on an empty box jumps to the previous box
 *   - Arrow keys navigate between boxes
 *   - Pasting a full code (any length ≤ `length`) fills from the first box
 *   - Non-numeric input is silently ignored
 *
 * @example
 * ```tsx
 * const [otp, setOtp] = useState("");
 * <OtpInput value={otp} onChange={setOtp} onComplete={(code) => verify(code)} />
 * ```
 */
export interface OtpInputProps {
  /** Current value (length may be 0..length). */
  value: string;
  /** Called on every change with the up-to-`length` digit string. */
  onChange: (value: string) => void;
  /** Optional callback fired once the user has entered exactly `length` digits. */
  onComplete?: (value: string) => void;
  /** Number of boxes to render. Default 6. */
  length?: number;
  /** Show red borders instead of the default. */
  error?: boolean;
  /** Disables all boxes. */
  disabled?: boolean;
  /** Auto-focuses the first empty box on mount. Default true. */
  autoFocus?: boolean;
  /** Optional label for accessibility tooling. */
  ariaLabel?: string;
}

export default function OtpInput({
  value,
  onChange,
  onComplete,
  length = 6,
  error = false,
  disabled = false,
  autoFocus = true,
  ariaLabel = "One-time password",
}: OtpInputProps) {
  // Exactly `length` refs — one per box.
  const inputRefs = React.useRef<Array<HTMLInputElement | null>>([]);

  // Focus the first empty box on mount (or the first box if value is empty).
  React.useEffect(() => {
    if (!autoFocus || disabled) return;
    const firstEmpty = Math.min(value.length, length - 1);
    inputRefs.current[firstEmpty]?.focus();
    // Only run on mount — callers can refocus by remounting if needed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fire onComplete when we cross the "full" threshold.
  const lastFiredRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (value.length === length && value !== lastFiredRef.current) {
      lastFiredRef.current = value;
      onComplete?.(value);
    }
    if (value.length < length) {
      lastFiredRef.current = null;
    }
  }, [value, length, onComplete]);

  // Render helper — what character sits in box N.
  function digitAt(i: number): string {
    return value[i] ?? "";
  }

  function updateDigit(i: number, digit: string) {
    // Only accept a single digit character.
    if (digit && !/^\d$/.test(digit)) return;

    const chars = value.split("");
    // Pad to index i so we can assign safely.
    while (chars.length < i) chars.push("");
    chars[i] = digit;
    // Trim any trailing empty slots so value length stays tight.
    let next = chars.join("");
    next = next.slice(0, length);
    // Strip trailing empty characters (in case we wrote a middle slot with
    // nothing after it — unusual but safe to normalize).
    next = next.replace(/(?:^|\s)\s+$/g, "");
    onChange(next);
  }

  function handleChange(e: React.ChangeEvent<HTMLInputElement>, index: number) {
    // Browsers sometimes give us multi-character input from paste / IME —
    // handle that by delegating to the paste logic.
    const raw = e.target.value;
    if (raw.length > 1) {
      handlePastedDigits(raw, index);
      return;
    }
    const digit = raw.slice(-1);
    if (digit === "") {
      // Clearing this box.
      const chars = value.split("");
      if (index < chars.length) {
        chars[index] = "";
      }
      onChange(chars.join(""));
      return;
    }
    if (!/^\d$/.test(digit)) return;
    updateDigit(index, digit);
    // Auto-advance.
    const nextIndex = index + 1;
    if (nextIndex < length) {
      inputRefs.current[nextIndex]?.focus();
      inputRefs.current[nextIndex]?.select();
    }
  }

  function handleKeyDown(
    e: React.KeyboardEvent<HTMLInputElement>,
    index: number
  ) {
    if (e.key === "Backspace") {
      if (digitAt(index)) {
        // Clear this box; stay put.
        const chars = value.split("");
        chars[index] = "";
        onChange(chars.join(""));
        return;
      }
      // Empty box — jump to previous box and clear it too.
      if (index > 0) {
        const prevIndex = index - 1;
        const chars = value.split("");
        chars[prevIndex] = "";
        onChange(chars.join(""));
        inputRefs.current[prevIndex]?.focus();
        e.preventDefault();
      }
      return;
    }
    if (e.key === "ArrowLeft" && index > 0) {
      inputRefs.current[index - 1]?.focus();
      e.preventDefault();
      return;
    }
    if (e.key === "ArrowRight" && index < length - 1) {
      inputRefs.current[index + 1]?.focus();
      e.preventDefault();
    }
  }

  function handlePastedDigits(raw: string, startIndex: number) {
    const digits = raw.replace(/\D/g, "").slice(0, length - startIndex);
    if (!digits) return;
    const chars = value.split("");
    while (chars.length < startIndex) chars.push("");
    for (let i = 0; i < digits.length; i++) {
      // charAt always returns string (empty string for out-of-range), which
      // satisfies TypeScript's noUncheckedIndexedAccess compared to digits[i].
      chars[startIndex + i] = digits.charAt(i);
    }
    const next = chars.join("").slice(0, length);
    onChange(next);
    // Focus the box after the last filled one (or the last box).
    const focusIndex = Math.min(startIndex + digits.length, length - 1);
    inputRefs.current[focusIndex]?.focus();
    inputRefs.current[focusIndex]?.select();
  }

  function handlePaste(
    e: React.ClipboardEvent<HTMLInputElement>,
    index: number
  ) {
    const pasted = e.clipboardData.getData("text");
    if (!pasted) return;
    e.preventDefault();
    handlePastedDigits(pasted, index);
  }

  const boxClass = cn(
    "w-10 h-12 text-center text-lg font-semibold rounded-md",
    "bg-background-neutral-01 border outline-none transition-colors",
    "focus:border-theme-primary-05",
    error ? "border-status-error-border-01" : "border-border-02",
    disabled && "opacity-50 cursor-not-allowed"
  );

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="flex items-center gap-2"
    >
      {Array.from({ length }, (_, i) => (
        <input
          key={i}
          ref={(el) => {
            inputRefs.current[i] = el;
          }}
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          autoComplete={i === 0 ? "one-time-code" : "off"}
          maxLength={length}
          disabled={disabled}
          value={digitAt(i)}
          onChange={(e) => handleChange(e, i)}
          onKeyDown={(e) => handleKeyDown(e, i)}
          onPaste={(e) => handlePaste(e, i)}
          onFocus={(e) => e.target.select()}
          className={boxClass}
          aria-label={`${ariaLabel} digit ${i + 1}`}
        />
      ))}
    </div>
  );
}
