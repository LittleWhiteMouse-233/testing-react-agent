import "@testing-library/jest-dom/vitest";

// jsdom has no pseudo-element layout; Ant Design uses it only to size scrollbars.
const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);
