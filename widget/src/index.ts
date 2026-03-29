/**
 * Naarni Chat Widget - Entry Point
 * Exports the main web component
 */

import { NaarniChatWidget } from "./widget";

// Define the custom element
if (
  typeof customElements !== "undefined" &&
  !customElements.get("naarni-chat-widget")
) {
  customElements.define("naarni-chat-widget", NaarniChatWidget);
}

// Export for use in other modules
export { NaarniChatWidget };
export * from "./types/api-types";
export * from "./types/widget-types";
