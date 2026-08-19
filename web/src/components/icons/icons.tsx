"use client";

import Image from "next/image";
import { StaticImageData } from "next/image";
import googleCloudStorageIcon from "@public/GoogleCloudStorage.png";
import openSourceIcon from "@public/OpenSource.png";
import r2Icon from "@public/r2.png";
import s3Icon from "@public/S3.png";
import boxIcon from "@public/Box.png";
import trelloIcon from "@public/Trello.png";
import serviceNowIcon from "@public/Servicenow.png";
import zAIIcon from "@public/Z_AI.png";

export interface IconProps {
  size?: number;
  className?: string;
}
export interface LogoIconProps extends IconProps {
  src: string | StaticImageData;
}

export const defaultTailwindCSS = "my-auto flex shrink-0 text-default";
export const defaultTailwindCSSBlue = "my-auto flex shrink-0 text-link";

export const LogoIcon = ({
  size = 16,
  className = defaultTailwindCSS,
  src,
}: LogoIconProps) => (
  <Image
    style={{ width: `${size}px`, height: `${size}px` }}
    className={`w-[${size}px] h-[${size}px] object-contain ` + className}
    src={src}
    alt="Logo"
    width="96"
    height="96"
  />
);

// Helper to create simple icon components from react-icon libraries
export function createIcon(
  IconComponent: React.ComponentType<{ size?: number; className?: string }>
) {
  function IconWrapper({
    size = 16,
    className = defaultTailwindCSS,
  }: IconProps) {
    return <IconComponent size={size} className={className} />;
  }

  IconWrapper.displayName = `Icon(${
    IconComponent.displayName || IconComponent.name || "Component"
  })`;
  return IconWrapper;
}

/**
 * Creates a logo icon component that automatically supports dark mode adaptations.
 *
 * Depending on the options provided, the returned component handles:
 * 1. Light/Dark variants: If both `src` and `darkSrc` are provided, displays the
 *    appropriate image based on the current color theme.
 * 2. Monochromatic inversion: If `monochromatic` is true, applies a CSS color inversion
 *    in dark mode for a monochrome icon appearance.
 * 3. Static icon: If only `src` is provided, renders the image without dark mode adaptation.
 *
 * @param src - The image or SVG source used for the icon (light/default mode).
 * @param options - Optional settings:
 *   - darkSrc: The image or SVG source used specifically for dark mode.
 *   - monochromatic: If true, applies a CSS inversion in dark mode for monochrome logos.
 *   - sizeAdjustment: Number to add to the icon size (e.g., 4 to make icon larger).
 *   - classNameAddition: Additional CSS classes to apply (e.g., '-m-0.5' for margin).
 * @returns A React functional component that accepts {@link IconProps} and renders
 *          the logo with dark mode handling as needed.
 */
const createLogoIcon = (
  src: string | StaticImageData,
  options?: {
    darkSrc?: string | StaticImageData;
    monochromatic?: boolean;
    sizeAdjustment?: number;
    classNameAddition?: string;
  }
) => {
  const {
    darkSrc,
    monochromatic,
    sizeAdjustment = 0,
    classNameAddition = "",
  } = options || {};

  const LogoIconWrapper = ({
    size = 16,
    className = defaultTailwindCSS,
  }: IconProps) => {
    const adjustedSize = size + sizeAdjustment;

    // Build className dynamically (only apply monochromatic if no darkSrc)
    const monochromaticClass = !darkSrc && monochromatic ? "dark:invert" : "";
    const finalClassName = [className, classNameAddition, monochromaticClass]
      .filter(Boolean)
      .join(" ");

    // If darkSrc is provided, use CSS-based dark mode switching
    // This avoids hydration issues and content flashing since next-themes
    // sets the .dark class before React hydrates
    if (darkSrc) {
      return (
        <>
          <LogoIcon
            size={adjustedSize}
            className={`${finalClassName} dark:hidden`}
            src={src}
          />
          <LogoIcon
            size={adjustedSize}
            className={`${finalClassName} hidden dark:block`}
            src={darkSrc}
          />
        </>
      );
    }

    return (
      <LogoIcon size={adjustedSize} className={finalClassName} src={src} />
    );
  };

  LogoIconWrapper.displayName = "LogoIconWrapper";
  return LogoIconWrapper;
};

// ============================================================================
// GENERIC SVG COMPONENTS (sorted alphabetically)
// ============================================================================
export const MacIcon = ({
  size = 16,
  className = "my-auto flex shrink-0 ",
}: IconProps) => {
  return (
    <svg
      style={{ width: `${size}px`, height: `${size}px` }}
      className={`w-[${size}px] h-[${size}px] ` + className}
      xmlns="http://www.w3.org/2000/svg"
      width="200"
      height="200"
      viewBox="0 0 24 24"
    >
      <path
        fill="currentColor"
        d="M6.5 4.5a2 2 0 0 1 2 2v2h-2a2 2 0 1 1 0-4Zm4 4v-2a4 4 0 1 0-4 4h2v3h-2a4 4 0 1 0 4 4v-2h3v2a4 4 0 1 0 4-4h-2v-3h2a4 4 0 1 0-4-4v2h-3Zm0 2h3v3h-3v-3Zm5-2v-2a2 2 0 1 1 2 2h-2Zm0 7h2a2 2 0 1 1-2 2v-2Zm-7 0v2a2 2 0 1 1-2-2h2Z"
      />
    </svg>
  );
};
export const NewChatIcon = ({
  size = 24,
  className = defaultTailwindCSS,
}: IconProps) => {
  return (
    <svg
      style={{ width: `${size}px`, height: `${size}px` }}
      className={`w-[${size}px] h-[${size}px] ` + className}
      viewBox="0 0 20 20"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M12.5 1.99982H6C3.79086 1.99982 2 3.79068 2 5.99982V13.9998C2 16.209 3.79086 17.9998 6 17.9998H14C16.2091 17.9998 18 16.209 18 13.9998V8.49982"
        stroke="currentColor"
        strokeLinecap="round"
      />
      <path
        d="M17.1471 5.13076C17.4492 4.82871 17.6189 4.41901 17.619 3.9918C17.6191 3.56458 17.4494 3.15484 17.1474 2.85271C16.8453 2.55058 16.4356 2.38082 16.0084 2.38077C15.5812 2.38071 15.1715 2.55037 14.8693 2.85242L11.0562 6.66651L7.24297 10.4806C7.1103 10.6129 7.01218 10.7758 6.95726 10.9549L6.20239 13.4418C6.18762 13.4912 6.18651 13.5437 6.19916 13.5937C6.21182 13.6437 6.23778 13.6894 6.27428 13.7258C6.31078 13.7623 6.35646 13.7881 6.40648 13.8007C6.45651 13.8133 6.509 13.8121 6.5584 13.7972L9.04585 13.0429C9.2248 12.9885 9.38766 12.891 9.52014 12.7589L17.1471 5.13076Z"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
};
export const NotebookIcon = ({
  size = 16,
  className = defaultTailwindCSS,
}: IconProps) => {
  return (
    <svg
      style={{ width: `${size}px`, height: `${size}px` }}
      className={`w-[${size}px] h-[${size}px] ` + className}
      xmlns="http://www.w3.org/2000/svg"
      width="200"
      height="200"
      viewBox="0 0 24 24"
    >
      <path
        fill="currentColor"
        d="M11.25 4.533A9.707 9.707 0 0 0 6 3a9.735 9.735 0 0 0-3.25.555a.75.75 0 0 0-.5.707v14.25a.75.75 0 0 0 1 .707A8.237 8.237 0 0 1 6 18.75c1.995 0 3.823.707 5.25 1.886V4.533Zm1.5 16.103A8.214 8.214 0 0 1 18 18.75c.966 0 1.89.166 2.75.47a.75.75 0 0 0 1-.708V4.262a.75.75 0 0 0-.5-.707A9.735 9.735 0 0 0 18 3a9.707 9.707 0 0 0-5.25 1.533v16.103Z"
      />
    </svg>
  );
};
export const NotebookIconSkeleton = ({
  size = 16,
  className = defaultTailwindCSS,
}: IconProps) => {
  return (
    <svg
      style={{ width: `${size}px`, height: `${size}px` }}
      className={`w-[${size}px] h-[${size}px] ` + className}
      xmlns="http://www.w3.org/2000/svg"
      width="200"
      height="200"
      viewBox="0 0 24 24"
    >
      <path
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
        d="M12 6.042A8.967 8.967 0 0 0 6 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 0 1 6 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 0 1 6-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0 0 18 18a8.967 8.967 0 0 0-6 2.292m0-14.25v14.25"
      />
    </svg>
  );
};
export const OnyxIcon = ({
  size = 16,
  className = defaultTailwindCSS,
}: IconProps) => {
  return (
    <svg
      style={{ width: `${size}px`, height: `${size}px` }}
      className={`w-[${size}px] h-[${size}px] ` + className}
      viewBox="0 0 106.545 120.0"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* NaArNi brand mark */}
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M48.93 0.04L1.88 27.2L0.95 27.96L0.49 28.59L0.11 29.5L0 30.23L0 87.91L0.13 88.72L0.48 89.54L0.95 90.18L1.88 90.93L51.29 119.46L52.38 119.9L53.47 120L54.47 119.81L55.56 119.27L102.31 92.27L90.07 85.19L55.1 105.38L54.56 105.63L53.83 105.82L52.74 105.83L52.02 105.65L51.11 105.2L14.59 84.12L13.5 83.4L12.74 82.46L12.39 81.64L12.23 80.73L12.23 37.4L12.3 36.86L12.56 36.04L13.28 34.95L14.14 34.29L49.02 14.15L49.06 0.07L49.02 0ZM57.47 0.02L57.44 8.61L57.62 10.25L57.98 11.61L58.61 13.06L59.35 14.24L60.46 15.53L61.55 16.44L62.55 17.08L94.28 35.41L94.28 72.46L94.41 73.83L94.58 74.64L94.96 75.83L95.67 77.28L96.58 78.55L97.89 79.85L99.34 80.85L106.51 84.95L106.5 28.32Z"
        fill="var(--theme-primary-05)"
      />
    </svg>
  );
};
export const OnyxLogoTypeIcon = ({
  size = 16,
  className = defaultTailwindCSS,
}: IconProps) => {
  // Horizontal lockup: mark + gap + wordmark, measured in mark-heights.
  const aspectRatio = 5.3236;
  const height = size / aspectRatio;

  return (
    <svg
      version="1.1"
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={height}
      viewBox="0 0 638.83 120.0"
      style={{ width: `${size}px`, height: `${height}px` }}
      className={`w-[${size}px] h-[${height}px] ` + className}
    >
      {/* NaArNi brand mark */}
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M48.93 0.04L1.88 27.2L0.95 27.96L0.49 28.59L0.11 29.5L0 30.23L0 87.91L0.13 88.72L0.48 89.54L0.95 90.18L1.88 90.93L51.29 119.46L52.38 119.9L53.47 120L54.47 119.81L55.56 119.27L102.31 92.27L90.07 85.19L55.1 105.38L54.56 105.63L53.83 105.82L52.74 105.83L52.02 105.65L51.11 105.2L14.59 84.12L13.5 83.4L12.74 82.46L12.39 81.64L12.23 80.73L12.23 37.4L12.3 36.86L12.56 36.04L13.28 34.95L14.14 34.29L49.02 14.15L49.06 0.07L49.02 0ZM57.47 0.02L57.44 8.61L57.62 10.25L57.98 11.61L58.61 13.06L59.35 14.24L60.46 15.53L61.55 16.44L62.55 17.08L94.28 35.41L94.28 72.46L94.41 73.83L94.58 74.64L94.96 75.83L95.67 77.28L96.58 78.55L97.89 79.85L99.34 80.85L106.51 84.95L106.5 28.32Z"
        fill="var(--theme-primary-05)"
      />
      {/* "NAARNI" wordmark, scaled down and vertically centered on the mark */}
      <g transform="translate(144.05, 33.00) scale(0.54000)">
        <path
          fillRule="evenodd"
          clipRule="evenodd"
          d="M20.55 0L17.61 0.31L13.42 1.52L10.06 3.22L6.71 5.79L2.74 10.9L1.17 14.26L0.28 17.61L0 20.13L0 99.79L26 99.79L26 29.77L26 29.35L26.42 29.24L96.02 89.81L104.82 97L109.43 99.04L113.63 99.88L119.5 100L123.69 99.55L127.88 98.25L131.66 96.13L134.17 94.1L136.85 90.99L139.67 85.53L140.74 80.08L140.74 0.42L140.46 0L115.3 0L114.74 0.42L114.74 70.44L114.47 71.02L36.9 3.66L32.71 1.5L28.51 0.28ZM229.35 0L158.49 99.84L189.52 99.97L203.61 79.25L204.61 78.27L269.18 78.27L269.6 77.99L261.63 66.75L257.86 63.57L252.83 61.13L246.96 60.1L216.86 59.96L243.6 20.24L297.32 99.79L330.44 99.79L259.96 0.29L259.54 0ZM423.89 0L421.68 2.52L352.81 99.79L383.64 100L398.74 78.29L463.73 78.26L463.94 77.99L463.42 77.15L456.35 67.09L451.57 63.15L446.95 61.05L441.5 60.1L411.16 59.96L436.73 21.8L437.73 20.38L438.15 20.34L491.82 99.95L524.76 99.79L454.08 0.12ZM549.68 0L549.26 0.42L549.26 99.79L577.77 99.79L577.77 19.71L578.19 19.5L646.11 19.5L652.82 20.39L657.85 22.5L660.03 24.32L661.81 26.84L662.88 30.61L662.88 33.12L662.22 36.06L660.93 38.58L657.43 41.7L653.66 43.35L649.05 44.24L609.22 44.3L603.35 45.2L598.31 47.64L594.04 51.57L592.3 54.09L590.68 57.86L589.93 63.31L590.35 63.59L645.69 63.59L649.89 64.01L653.24 64.81L655.76 66.09L657.24 67.51L658.43 69.6L658.83 71.28L658.97 99.79L687.34 99.79L687.34 72.96L686.03 66.25L683.57 61.64L680.83 58.7L677.56 56.42L672.28 54.09L680.07 50.04L685.11 45.53L688.6 40.25L689.77 37.32L690.64 33.54L690.98 30.19L690.77 25.58L689.02 18.87L686.99 15.1L684.77 12.16L680.49 8.3L675.46 5.27L669.17 2.77L662.05 1.1L654.92 0.22ZM740.45 0L737.1 0.34L732.9 1.54L729.55 3.24L726.2 5.79L722.21 10.9L720.66 14.26L719.77 17.61L719.49 19.71L719.49 99.79L745.48 99.79L745.49 29.35L745.9 29.21L821.37 94.9L824.31 97L828.92 99.04L833.11 99.91L839.4 100L843.59 99.49L847.37 98.25L851.14 96.13L853.66 94.1L856.17 91.2L858.85 86.37L859.67 83.86L860.23 80.08L860.23 0.42L859.95 0L834.79 0L834.23 0.42L834.23 70.44L833.95 71.03L758.06 4.95L754.29 2.43L750.09 0.79L745.06 0ZM888.46 0L888.04 0.42L888.04 99.79L916.13 99.93L916.27 20.13L915.45 15.52L913.75 11.32L911.18 7.55L909 5.37L905.65 2.93L901.87 1.17L898.52 0.28Z"
          fill="var(--theme-primary-05)"
        />
      </g>
    </svg>
  );
};
export const WindowsIcon = ({
  size = 16,
  className = "my-auto flex shrink-0 ",
}: IconProps) => {
  return (
    <svg
      style={{ width: `${size}px`, height: `${size}px` }}
      className={`w-[${size}px] h-[${size}px] ` + className}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      width="24"
      height="24"
    >
      <path
        fill="currentColor"
        d="M3 3h8v8H3V3zm10 0h8v8h-8V3zm-10 10h8v8H3v-8zm10 0h8v8h-8v-8z"
      />
    </svg>
  );
};

// ============================================================================
// THIRD-PARTY / COMPANY ICONS (Alphabetically)
// Only icons that don't yet have opal logo equivalents remain here.
// ============================================================================
export const BoxIcon = createLogoIcon(boxIcon);
export const GoogleStorageIcon = createLogoIcon(googleCloudStorageIcon, {
  sizeAdjustment: 4,
  classNameAddition: "-m-0.5",
});
export const OpenSourceIcon = createLogoIcon(openSourceIcon);
export const R2Icon = createLogoIcon(r2Icon);
export const S3Icon = createLogoIcon(s3Icon);
export const ServiceNowIcon = createLogoIcon(serviceNowIcon);
export const TrelloIcon = createLogoIcon(trelloIcon);
export const ZAIIcon = createLogoIcon(zAIIcon);
