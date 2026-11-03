import { useLayoutEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

// Only reading pages opt in; directory anchors retain native browser scrolling.
export default function useReadingPageScroll() {
  const { pathname, hash } = useLocation();
  const enteredPath = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (enteredPath.current === pathname) return;
    enteredPath.current = pathname;
    if (!hash) window.scrollTo(0, 0);
  }, [pathname, hash]);
}
