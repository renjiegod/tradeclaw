import "@testing-library/jest-dom/vitest";
import { beforeAll } from "vitest";

// jsdom lacks matchMedia, which antd's responsive hooks (Grid/Dropdown/Avatar)
// call during render. Provide a default here so any antd component test can
// render; individual tests may still override it.
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// jsdom does not implement Range.getClientRects which CodeMirror requires
const mockDOMRect = { x: 0, y: 0, width: 0, height: 0, top: 0, right: 0, bottom: 0, left: 0 };
const mockDOMRectList = [mockDOMRect] as unknown as DOMRectList;

beforeAll(() => {
  if (typeof Range !== "undefined") {
    Range.prototype.getClientRects = () => mockDOMRectList;
    Range.prototype.getBoundingClientRect = () => mockDOMRect;
  }
});
