import { withBasePath } from '@/lib/paths';

type BrandIconSize = 32 | 48 | 96 | 180 | 512;

// Versioned local assets work offline and keep root and gateway builds aligned.
export function brandIconPath(size: BrandIconSize): string {
  return withBasePath(`/brand/cangzhi-icon-v1-${size}.png`);
}

export const BRAND_ICONS = {
  icon: [
    { url: brandIconPath(32), sizes: '32x32', type: 'image/png' },
    { url: brandIconPath(48), sizes: '48x48', type: 'image/png' },
  ],
  apple: [{ url: brandIconPath(180), sizes: '180x180', type: 'image/png' }],
};
