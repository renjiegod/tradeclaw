/**
 * Attempts to parse a value as a JSON string representing an object.
 * Returns the parsed object if the value is a string containing valid JSON
 * for an object (not array, not primitive), otherwise returns null.
 */
export function tryParseJsonString(
  value: unknown
): Record<string, unknown> | null {
  if (typeof value !== "string") {
    return null;
  }

  try {
    const parsed = JSON.parse(value);
    // Only return objects, not arrays or primitives
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}
