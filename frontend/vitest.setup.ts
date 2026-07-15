import "@testing-library/jest-dom/vitest";
import { beforeAll } from "vitest";

// jsdom does not implement Range.getClientRects which CodeMirror requires
const mockDOMRect = { x: 0, y: 0, width: 0, height: 0, top: 0, right: 0, bottom: 0, left: 0 };
const mockDOMRectList = [mockDOMRect] as unknown as DOMRectList;

beforeAll(() => {
  if (typeof Range !== "undefined") {
    Range.prototype.getClientRects = () => mockDOMRectList;
    Range.prototype.getBoundingClientRect = () => mockDOMRect;
  }
});
