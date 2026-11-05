/**
 * OnlyYou Plugin Pages — 内联 SVG 线性图标库 (`views/console/icons.js`)
 *
 * 无构建、无外部依赖。所有图标统一 24x24 viewBox、`stroke="currentColor"`，
 * 颜色随父元素文字色变化，可直接用于按钮 / 导航 / 空状态 / 卡片标题。
 *
 * 用法：
 *   element.innerHTML = icon("refresh", 16);
 *   element.innerHTML = window.OY_ICONS.calendar;   // 默认 16px
 */
(function () {
  "use strict";

  var PATHS = {
    /* ---- 导航 / 页面 ---- */
    gauge:
      '<path d="M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"/><path d="m13.4 10.6 4.1-4.1"/><path d="M3.5 18a9 9 0 1 1 17 0"/>',
    calendar:
      '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/><path d="M8 14h2M14 14h2M8 17h2"/>',
    character:
      '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-3.3 3.1-6 7-6s7 2.7 7 6"/><path d="M8.5 5.5C9 4 10.3 3 12 3s3 1 3.5 2.5"/>',
    relations:
      '<circle cx="9" cy="8" r="3"/><circle cx="17.5" cy="9.5" r="2.5"/><path d="M3 20c0-3 2.7-5 6-5s6 2 6 5"/><path d="M16 20c0-1.8 1.3-3.5 3.5-3.9"/>',
    memory:
      '<path d="M12 3a4 4 0 0 0-4 4v1a3 3 0 0 0 0 6v1a4 4 0 0 0 8 0v-1a3 3 0 0 0 0-6V7a4 4 0 0 0-4-4Z"/><path d="M12 6.5v11"/>',
    surf:
      '<path d="M3 16.5c2 1 4 1 6 0s4-1 6 0 4 1 6 0"/><path d="M3 20.5c2 1 4 1 6 0s4-1 6 0 4 1 6 0"/><path d="M15 4.5 6 13.5V17h3.5l9-9-3.5-3.5Z"/><path d="m13 6.5 3.5 3.5"/>',
    models:
      '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/><path d="M7.5 8h4M7.5 11h7"/>',
    sessions:
      '<path d="M21 11.5a8 8 0 0 1-8 8H7.5L3 22v-10.5a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"/><path d="M8 9.5h8M8 13h5"/>',
    dashboard:
      '<rect x="3" y="3" width="8" height="10" rx="1.5"/><rect x="13" y="3" width="8" height="6" rx="1.5"/><rect x="13" y="11" width="8" height="10" rx="1.5"/><rect x="3" y="15" width="8" height="6" rx="1.5"/>',

    /* ---- 操作 ---- */
    refresh:
      '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash:
      '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/><path d="M10 11v5.5M14 11v5.5"/>',
    edit:
      '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z"/>',
    check: '<path d="M4 12.5 9.5 18 20 6"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    save: '<path d="M5 3h11l3 3v15H5V3Z"/><path d="M8.5 3v5h7V3"/><path d="M8 21v-7h8v7"/>',
    wand:
      '<path d="m3 21 9-9"/><path d="m14 3 1.6 3.4L19 8l-3.4 1.6L14 13l-1.6-3.4L9 8l3.4-1.6L14 3Z"/><path d="M19.5 14.5 20 16l1.5.5-1.5.5-.5 1.5-.5-1.5L17 16.5l1.5-.5.5-1.5Z"/>',
    play: '<path d="M7 4v16l13-8L7 4Z"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 21h16"/>',
    upload: '<path d="M12 21V9M7 14l5-5 5 5"/><path d="M4 3h16"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4.2-4.2"/>',
    copy:
      '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
    filter: '<path d="M3 5h18M6 12h12M10 19h4"/>',
    external:
      '<path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    link:
      '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.5 1.5"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5"/>',

    /* ---- 主题 / 状态 ---- */
    moon: '<path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z"/>',
    sun:
      '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    shield:
      '<path d="M12 3 4 6v6c0 4.4 3.4 7.8 8 9 4.6-1.2 8-4.6 8-9V6l-8-3Z"/><path d="m8.5 12 2.4 2.4 4.6-4.8"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    spark: '<path d="m12 3 2.6 5.4 6 .9-4.3 4.2 1 5.9L12 16.6l-5.3 2.8 1-5.9L3.4 9.3l6-.9L12 3Z"/>',
    zap: '<path d="m13 2-9 12h7l-1 8 9-12h-7l1-8Z"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    alert: '<path d="M12 3 2 21h20L12 3Z"/><path d="M12 9v5M12 17.5v.5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.4v.6"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
    video:
      '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3V9Z"/>',
    heart:
      '<path d="M12 20s-7-4.4-7-9.3C5 7.4 7.2 5.5 9.5 5.5c1.4 0 2.2.8 2.5 1.5.3-.7 1.1-1.5 2.5-1.5 2.3 0 4.5 1.9 4.5 5.2C19 15.6 12 20 12 20Z"/>',
    volume:
      '<path d="M4 10v4h4l5 4V6l-5 4H4Z"/><path d="M16.5 9.2a4 4 0 0 1 0 5.6M19 6.5a7.5 7.5 0 0 1 0 11"/>',
    database:
      '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    key:
      '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 3l3 3M14 6l3 3"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.5M3.5 12h.5M3.5 18h.5"/>',
    grid:
      '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    dots:
      '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
    chevron: '<path d="m9 6 6 6-6 6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "arrow-left": '<path d="M19 12H5M11 18l-6-6 6-6"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>'
  };

  function buildSvg(name, size) {
    var body = PATHS[name] || PATHS.info;
    var px = parseInt(size, 10) || 16;
    return (
      '<svg class="oy-icon" width="' +
      px +
      '" height="' +
      px +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
      body +
      "</svg>"
    );
  }

  var icons = {};
  Object.keys(PATHS).forEach(function (name) {
    icons[name] = buildSvg(name, 16);
  });

  window.OY_ICON_PATHS = PATHS;
  window.OY_ICONS = icons;
  window.OY_ICON_NAMES = Object.keys(PATHS);
  window.icon = function (name, size) {
    return buildSvg(name, size || 16);
  };
})();
