import Image from 'next/image';

import { brandIconPath } from '@/lib/brand';

type BrandMarkProps = {
  size?: 36 | 44;
  className?: string;
};

// The adjacent brand text or enclosing link supplies the accessible name.
export function BrandMark({ size = 36, className = '' }: BrandMarkProps) {
  return (
    <Image
      src={brandIconPath(96)}
      alt=""
      aria-hidden="true"
      data-brand-mark="cangzhi"
      width={size}
      height={size}
      unoptimized
      loading="eager"
      className={`block shrink-0 object-contain ${size === 44 ? 'h-11 w-11' : 'h-9 w-9'} ${className}`}
    />
  );
}
