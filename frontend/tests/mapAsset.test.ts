import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const asset = readFileSync(resolve(process.cwd(), 'public/assets/maps/mirage.svg'), 'utf8');

describe('authorized Mirage map asset', () => {
  it('is self-contained, safe, and preserves the source aspect ratio', () => {
    const document = new DOMParser().parseFromString(asset, 'image/svg+xml');
    expect(document.querySelector('parsererror')).toBeNull();
    expect(document.documentElement.getAttribute('viewBox')).toBe('0 0 1000 1000');
    expect(asset).not.toMatch(/<script|<foreignObject|\son[a-z]+\s*=/i);

    const image = document.querySelector('image');
    expect(image).not.toBeNull();
    const href = image!.getAttribute('href') ?? '';
    expect(href.startsWith('data:image/webp;base64,')).toBe(true);
    const bytes = Buffer.from(href.slice('data:image/webp;base64,'.length), 'base64');
    expect(bytes).toHaveLength(124_250);
    expect(createHash('sha256').update(bytes).digest('hex')).toBe(
      '91294b8d9b963c898cca18c774e4965b6fbe95244d2630e6e087c919433e348f',
    );

    expect(Number(image!.getAttribute('x'))).toBe(0);
    expect(Number(image!.getAttribute('width'))).toBe(1000);
    expect(Number(image!.getAttribute('height'))).toBeCloseTo((1196 / 1374) * 1000, 3);
    expect(Number(image!.getAttribute('y'))).toBeCloseTo((1000 - (1196 / 1374) * 1000) / 2, 3);
    expect(image!.getAttribute('preserveAspectRatio')).toBe('xMidYMid meet');
    expect(asset).not.toContain('PLACEHOLDER — NOT FOR PUBLIC RELEASE');
    expect(asset).not.toContain('核验中');
  });
});
