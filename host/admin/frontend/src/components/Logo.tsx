import { useTheme } from "../lib/theme";

interface Props { size?: number; className?: string }

export function Logo({ size = 26, className }: Props) {
  const { theme } = useTheme();
  const src = theme === "light" ? "/logo-light.svg" : "/logo-dark.svg";
  return (
    <img
      src={src}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      className={className}
    />
  );
}
