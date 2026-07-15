import type { ConsolePageKey } from "../types";

/**
 * Maps URL pathname to sidebar menu key. Task detail URLs stay under "tasks".
 * The default landing page is the Chat (assistant) view.
 */
export function menuKeyFromPathname(pathname: string): ConsolePageKey {
  if (pathname.startsWith("/agents")) {
    return "agents";
  }
  if (pathname.startsWith("/cron_jobs")) {
    return "cron_jobs";
  }
  if (pathname.startsWith("/channels")) {
    return "channels";
  }
  if (pathname.startsWith("/assistant")) {
    return "assistant";
  }
  if (pathname.startsWith("/swarm")) {
    return "swarm";
  }
  if (pathname.startsWith("/tasks")) {
    return "tasks";
  }
  if (pathname.startsWith("/accounts")) {
    return "accounts";
  }
  if (pathname.startsWith("/stock_monitor")) {
    return "stock_monitor";
  }
  if (pathname.startsWith("/market_review")) {
    return "market_review";
  }
  if (pathname.startsWith("/stocks")) {
    return "stocks";
  }
  if (pathname.startsWith("/watchlist")) {
    return "watchlist";
  }
  if (pathname.startsWith("/approvals")) {
    return "approvals";
  }
  if (pathname.startsWith("/strategies")) {
    return "strategies";
  }
  if (pathname.startsWith("/knowledge")) {
    return "knowledge";
  }
  if (pathname.startsWith("/model_invocations")) {
    return "model_invocations";
  }
  if (pathname.startsWith("/settings/models")) {
    return "settings_models";
  }
  if (pathname.startsWith("/settings")) {
    return "settings";
  }
  return "assistant";
}
