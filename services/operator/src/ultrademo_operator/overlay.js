// Injected into every page of the sandbox (context.add_init_script) so the streamed video shows a
// cursor and highlights even before the player draws its own client-side overlays (ADR 4). Lives in
// a closed shadow root with pointer-events:none so it cannot affect the product's layout or clicks.
(() => {
  if (window.top !== window || window.__ultrademo) return;
  const state = { host: null, cursor: null, box: null, timer: null };

  function mount() {
    if (state.host || !document.documentElement) return;
    const host = document.createElement("ultrademo-overlay");
    host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
    const root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `
      <style>
        .c{position:fixed;width:22px;height:22px;margin:-3px 0 0 -3px;transition:transform 60ms linear;
           filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}
        .b{position:fixed;border:3px solid #0F7F82;border-radius:8px;box-shadow:0 0 0 9999px rgba(15,23,35,.18);
           transition:all 160ms ease-out;opacity:0}
        .b span{position:absolute;left:-3px;top:-30px;background:#0F7F82;color:#fff;font:600 13px/1.2 system-ui;
           padding:5px 8px;border-radius:6px;white-space:nowrap}
      </style>
      <svg class="c" viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.4-7.6L20.5 11z" fill="#fff" stroke="#151B23" stroke-width="1.6" stroke-linejoin="round"/></svg>
      <div class="b"><span></span></div>`;
    state.cursor = root.querySelector(".c");
    state.box = root.querySelector(".b");
    state.cursor.style.transform = "translate(-40px,-40px)";
    document.documentElement.appendChild(host);
    state.host = host;
  }

  document.addEventListener("mousemove", (e) => {
    mount();
    if (state.cursor) state.cursor.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
  }, { capture: true, passive: true });

  window.__ultrademo = {
    highlight(b) {
      mount();
      const s = state.box.style;
      s.left = `${b.x - 6}px`; s.top = `${b.y - 6}px`; s.width = `${b.width + 6}px`; s.height = `${b.height + 6}px`;
      s.opacity = "1";
      const label = state.box.querySelector("span");
      label.textContent = b.label || "";
      label.style.display = b.label ? "block" : "none";
      clearTimeout(state.timer);
      state.timer = setTimeout(() => { s.opacity = "0"; }, 4000);
    },
    clear() { if (state.box) state.box.style.opacity = "0"; },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
