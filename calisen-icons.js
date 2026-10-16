/* Geko icon swapper (presentation only). Replaces emoji in visible text with consistent line icons.
   The original emoji character stays in the DOM (visually hidden), so textContent-based logic is unaffected. */
(function () {
  var MAP = {"📅": "calendar", "📆": "calendar", "📊": "chart", "📈": "trend", "📝": "file", "📄": "file", "📃": "file", "📋": "clipboard", "🖼": "image", "📷": "image", "📎": "paperclip", "🔍": "search", "🎥": "video", "🚫": "ban", "⚡": "zap", "🚀": "arrow-up-right", "🔙": "undo", "👤": "user", "👷": "user", "👥": "users", "🔧": "wrench", "✅": "check-circle", "📞": "phone", "❌": "x-circle", "📌": "pin", "📍": "pin", "🎯": "target", "📤": "upload", "🔒": "lock", "🚗": "car", "⚙": "settings", "🔑": "key", "🌴": "sun", "🤒": "thermometer", "🎓": "graduation", "💾": "save", "🗺": "map", "⏰": "clock", "💷": "pound", "📭": "inbox", "⚠": "alert", "🎉": "sparkles", "❓": "help", "🏠": "home", "☑": "check-circle", "⛔": "ban", "🛑": "ban", "📦": "inbox", "✨": "sparkles", "🔔": "alert", "🔥": "alert", "💡": "sparkles", "🏢": "home", "📧": "inbox", "✉": "inbox"};
  var RE = new RegExp('(' + Object.keys(MAP).join('|') + ')\\uFE0F?', 'g');
  var SKIP = /^(SCRIPT|STYLE|TEXTAREA|OPTION|TITLE|INPUT|SELECT|CODE|PRE)$/;
  function walk(root) {
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n, hits = [];
    while ((n = w.nextNode())) {
      var p = n.parentNode;
      if (!p || SKIP.test(p.nodeName) || (p.closest && p.closest('.theme-icon,[data-no-icons],.gk-i'))) continue;
      RE.lastIndex = 0;
      if (RE.test(n.nodeValue)) hits.push(n);
    }
    hits.forEach(function (node) {
      var text = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m;
      RE.lastIndex = 0;
      while ((m = RE.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var s = document.createElement('span');
        s.className = 'gk-i gk-i-' + MAP[m[1]]; s.setAttribute('aria-hidden', 'true'); s.textContent = m[0];
        frag.appendChild(s); last = m.index + m[0].length;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  }
  var raf = 0, queue = new Set();
  function flush() { raf = 0; var items = Array.from(queue); queue.clear(); items.forEach(function (el) { if (el && el.isConnected) walk(el); }); }
  function start() {
    walk(document.body);
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (a) { var t = a.nodeType === 1 ? a : a.parentNode; if (t) queue.add(t); });
        if (m.type === 'characterData' && m.target.parentNode) queue.add(m.target.parentNode);
      });
      if (queue.size && !raf) raf = requestAnimationFrame(flush);
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
