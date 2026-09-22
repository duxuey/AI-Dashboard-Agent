"use strict";

// ---- 系统注册表（列表页 / 详情页 / 统计页共用）----
// 三个页面都要把 source key 翻成中文名，也都要处理「注册表里没有这个 key」。
// 这套规则只写这一份，否则三处各写一套，加新系统时必然漏掉某一处。

window.Systems = (function () {
  let loading = null;
  const table = {};        // { key: 显示名 }

  // 幂等：三个页面各调一次也只发一个请求
  function load() {
    if (!loading) {
      loading = fetch("/api/systems")
        .then((res) => (res.ok ? res.json() : { systems: [] }))
        .then((data) => {
          for (const s of data.systems || []) table[s.key] = s.label;
          return table;
        })
        // 拿不到注册表也照样能用：全部退化成显示原始 key
        .catch(() => table);
    }
    return loading;
  }

  // 未注册的 key 原样返回——新项目可以先上报、后补显示名，不该因此不显示。
  // 空值统一给 "-"，与模型列缺失时的写法一致。
  function labelOf(key) {
    if (!key) return "-";
    return table[key] || key;
  }

  // 下拉选项：注册表里的在前（顺序 = .env 声明顺序），
  // 数据里出现过但没注册的追加在后，保证未注册来源也能被筛出来
  function options(observedKeys) {
    const out = Object.keys(table).map((k) => ({ value: k, label: table[k] }));
    const known = new Set(out.map((o) => o.value));
    for (const k of observedKeys || []) {
      if (k && !known.has(k)) {
        out.push({ value: k, label: k });
        known.add(k);
      }
    }
    return out;
  }

  // 注册表本体（设置页要把它整张列出来，而不是查单个 key）
  function list() {
    return Object.keys(table).map((k) => ({ key: k, label: table[k] }));
  }

  return { load, labelOf, options, list };
})();
