export function decodeRequestPath(pathname) {
  try {
    return decodeURIComponent(pathname);
  } catch {
    return null;
  }
}
