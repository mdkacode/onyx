import type { IconProps } from "@opal/types";

// NaArNi brand mark — three green hexagons with a gradient. Replaces the
// upstream Onyx logo across every @opal/logos surface (sidebar, header, etc.).
const SvgOnyxLogo = ({ size, ...props }: IconProps) => (
  <svg
    height={size}
    viewBox="12 3 76 74"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    {...props}
  >
    <defs>
      <linearGradient id="naarniGrad" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stopColor="#7EEDB4" />
        <stop offset="100%" stopColor="#2ECC71" />
      </linearGradient>
    </defs>
    {/* Top hexagon */}
    <polygon points="50,5 68,15 68,35 50,45 32,35 32,15" fill="url(#naarniGrad)" />
    {/* Bottom-left hexagon */}
    <polygon points="32,35 50,45 50,65 32,75 14,65 14,45" fill="url(#naarniGrad)" />
    {/* Bottom-right hexagon */}
    <polygon points="68,35 86,45 86,65 68,75 50,65 50,45" fill="url(#naarniGrad)" />
    {/* Center highlight */}
    <polygon points="50,35 58,40 58,50 50,55 42,50 42,40" fill="white" />
  </svg>
);
export default SvgOnyxLogo;
