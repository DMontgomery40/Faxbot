import { qrMatrix } from './qr';

const QUIET_ZONE = 4;

// Scannable QR code for a short numeric value, drawn as an SVG.
export default function QrCode({ value, size = 200, label }: { value: string; size?: number; label: string }) {
  const matrix = qrMatrix(value);
  if (!matrix) return null;
  const extent = matrix.length + QUIET_ZONE * 2;
  let path = '';
  matrix.forEach((row, y) => row.forEach((dark, x) => {
    if (dark) path += `M${x + QUIET_ZONE} ${y + QUIET_ZONE}h1v1h-1z`;
  }));
  return (
    <svg role="img" aria-label={label} width={size} height={size} viewBox={`0 0 ${extent} ${extent}`} shapeRendering="crispEdges">
      <rect width={extent} height={extent} fill="#ffffff" />
      <path d={path} fill="#000000" />
    </svg>
  );
}
