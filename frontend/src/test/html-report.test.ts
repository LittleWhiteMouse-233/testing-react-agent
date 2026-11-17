import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const template = readFileSync(resolve("../backend/app/templates/report.html"), "utf8");
const script = template.match(/<script>([\s\S]*?)<\/script>/)![1]!;
let heights: Map<Element, number>;
let notifyResize: (target: Element) => void;

beforeEach(() => {
  document.body.innerHTML = `<details class="task-events"><summary>任务一</summary>
    <div class="message-block"><div id="long-text" class="message-text message-preview"></div>
      <div class="message-actions" hidden><button class="message-toggle" type="button" aria-controls="long-text" aria-expanded="false">展开</button><span class="message-count">共 18 字</span></div></div>
    <div class="message-block"><div id="short-text" class="message-text message-preview">短消息</div>
      <div class="message-actions" hidden><button class="message-toggle" type="button" aria-controls="short-text" aria-expanded="false">展开</button><span class="message-count">共 3 字</span></div></div>
    </details>`;
  document.getElementById("long-text")!.textContent = "首段 😀 \r\n第二行\n第三行\n第四行";
  heights = new Map();
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.closest("details")?.open ? 200 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return heights.get(this) ?? (this.textContent ?? "").split("\n").length * 20;
  });
  const computedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) => new Proxy(computedStyle(element), {
    get: (target, property) => property === "lineHeight" ? "20px" : Reflect.get(target, property)
  }));
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) {
      notifyResize = (target) => callback([{ target } as ResizeObserverEntry], this as unknown as ResizeObserver);
    }
    observe() {}
  });
  new Function(script)();
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("measures offline text after its task opens and keeps each block independent", () => {
  const group = document.querySelector("details")!;
  const longText = document.getElementById("long-text")!;
  const shortText = document.getElementById("short-text")!;
  const actions = longText.nextElementSibling as HTMLElement;
  const button = actions.querySelector("button")!;
  const originalText = longText.textContent;
  expect(actions.hidden).toBe(true);
  group.open = true;
  notifyResize(longText); notifyResize(shortText);
  expect(actions.hidden).toBe(false);
  expect((shortText.nextElementSibling as HTMLElement).hidden).toBe(true);
  button.click();
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(longText).not.toHaveClass("message-preview");
  expect(shortText).toHaveClass("message-preview");
  expect(actions.querySelector(".message-count")).toHaveTextContent("共 18 字");
  button.click();
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(longText).toHaveClass("message-preview");
  expect(longText.textContent).toBe(originalText);
});

it("retains expansion across width changes and re-opening its offline task", () => {
  const group = document.querySelector("details")!;
  const text = document.getElementById("long-text")!;
  const actions = text.nextElementSibling as HTMLElement;
  const button = actions.querySelector("button")!;
  group.open = true; notifyResize(text); button.click();
  heights.set(text, 40); notifyResize(text);
  expect(actions.hidden).toBe(true);
  heights.set(text, 100); notifyResize(text);
  expect(actions.hidden).toBe(false);
  expect(button).toHaveTextContent("收起");
  group.open = false; notifyResize(text);
  group.open = true; notifyResize(text);
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(text).not.toHaveClass("message-preview");
});
