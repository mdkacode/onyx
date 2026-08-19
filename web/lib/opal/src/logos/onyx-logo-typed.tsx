import SvgOnyxLogo from "@opal/logos/onyx-logo";
import SvgOnyxTyped from "@opal/logos/onyx-typed";
import { cn } from "@opal/utils";

interface OnyxLogoTypedProps {
  size?: number;
  className?: string;
}

// # NOTE(@raunakab):
// This ratio is not some random, magical number; it is available on Figma.
const HEIGHT_TO_GAP_RATIO = 5 / 16;

// The NaArNi wordmark is much wider than it is tall, so it is set to a
// fraction of the mark's height rather than matching it. Mirrors the
// proportions of the stacked lockup in the brand assets.
const HEIGHT_TO_WORDMARK_RATIO = 0.45;

const SvgOnyxLogoTyped = ({ size: height, className }: OnyxLogoTypedProps) => {
  const gap = height != null ? height * HEIGHT_TO_GAP_RATIO : undefined;
  const wordmarkHeight =
    height != null ? height * HEIGHT_TO_WORDMARK_RATIO : undefined;

  return (
    <div
      className={cn(`flex flex-row items-center`, className)}
      style={{ gap }}
    >
      <SvgOnyxLogo size={height} />
      <SvgOnyxTyped size={wordmarkHeight} />
    </div>
  );
};
export default SvgOnyxLogoTyped;
