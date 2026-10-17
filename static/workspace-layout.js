/* Presentation only: preserve existing nodes, data, controls and permissions. */
(() => {
  'use strict';
  const layouts = Object.freeze({
    dashboard: {kind:'overview', sections:[['#majorMarketOverview','大盘风向'],['.opportunity-signal-grid','结构候选'],['.table-wrap','跨所报价']]},
    'onchain-opportunities': {kind:'discovery'},
    'spot-futures': {kind:'market'},
    'dual-futures': {kind:'market'},
    'my-positions': {kind:'ledger'},
    'funding-trend': {kind:'funding'},
    'trade-validation': {kind:'validation', sections:[['#pushSignalValidation','推送验证'],['#tradeValidation','模拟交易']]},
    thinking: {kind:'thinking'},
    'daily-trends': {kind:'trends', sections:[['.early-focus-panel','启动前观察'],['#dailyHorn30m','30MIN结构'],['#dailyHorn4h','4H结构'],['#dailyHornContinue','延续观察']]},
    'daily-thoughts': {kind:'research', sections:[['.thought-watch-manager','盯盘管理'],['.thought-record-list','思路复盘']]},
    'daily-listings': {kind:'listings'},
    'gainers-losers': {kind:'rankings'},
    'token-hedge': {kind:'evidence'},
    alerts: {kind:'alerts', sections:[['#alertEvents','报警时间线'],['#basisTracking','基差追踪'],['#alertMuteList','静音管理']]},
    chat: {kind:'conversation'},
    'security-notifications': {kind:'security'},
    strategies: {kind:'strategies'},
    'runtime-rules': {kind:'settings'},
    'account-management': {kind:'accounts', sections:[['#accountUserList','账号与设备'],['#accountInviteList','邀请码']]},
    risk: {kind:'risk'},
  });

  function accessibleScroll(node, label) {
    if (!node || node.dataset.layoutScroll) return;
    node.dataset.layoutScroll = 'true';
    node.tabIndex = 0;
    node.setAttribute('role', 'region');
    node.setAttribute('aria-label', label);
  }

  function groupFundingMetrics(grid) {
    const values = [...grid.children];
    if (values.length !== 12 || values.some(node => node.tagName !== 'SPAN')) return;
    const groups = [
      ['价格与量仓', [0, 1, 8, 9]],
      ['资金费率', [2, 3, 4, 5]],
      ['对冲与流动性', [6, 7, 10, 11]],
    ];
    for (const [title, indexes] of groups) {
      const section = document.createElement('section');
      section.className = 'layout-metric-group';
      const heading = document.createElement('h4');
      heading.textContent = title;
      const body = document.createElement('div');
      body.className = 'layout-metric-values';
      indexes.forEach(index => body.appendChild(values[index]));
      section.append(heading, body);
      grid.appendChild(section);
    }
  }

  function prepareRankingScroll(view) {
    view.querySelectorAll('.daily-grid > .panel').forEach((panel, index) => {
      const head = panel.querySelector('.gainer-table-head');
      const list = panel.querySelector('.gainer-table');
      if (!head || !list || head.parentElement.classList.contains('layout-ranking-scroll')) return;
      const title = document.createElement('h3');
      title.className = 'layout-ranking-title';
      title.textContent = index === 0 ? '涨幅靠前' : '跌幅靠前';
      const scroll = document.createElement('div');
      scroll.className = 'layout-ranking-scroll';
      panel.insertBefore(title, head);
      panel.insertBefore(scroll, head);
      scroll.append(head, list);
      accessibleScroll(scroll, `${title.textContent}，六列行情对照，可横向滚动`);
    });
  }

  function foldReadingGuide(node, title) {
    if (!node || node.parentElement.classList.contains('layout-reading-guide')) return;
    const details = document.createElement('details');
    details.className = 'layout-reading-guide';
    const summary = document.createElement('summary');
    summary.textContent = title;
    node.before(details);
    details.append(summary, node);
  }

  function prepareOutline(view, profile) {
    let sections = profile.sections || [];
    if (view.id === 'runtime-rules') {
      sections = [...view.querySelectorAll('.runtime-rule-group')].map((group, index) => {
        group.dataset.layoutRule = String(index);
        return [`[data-layout-rule="${index}"]`, group.querySelector('h3')?.textContent || `规则组 ${index + 1}`];
      });
    }
    sections = sections.filter(([selector]) => view.querySelector(selector));
    if (sections.length < 2) return;
    const signature = sections.map(item => item.join(':')).join('|');
    let nav = view.querySelector(':scope > .layout-section-outline');
    if (nav?.dataset.signature === signature) return;
    if (!nav) {
      nav = document.createElement('nav');
      nav.className = 'layout-section-outline';
      nav.setAttribute('aria-label', '本栏内容导航');
      view.prepend(nav);
    }
    nav.replaceChildren();
    nav.dataset.signature = signature;
    for (const [selector, title] of sections) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = title;
      button.addEventListener('click', () => {
        const target = view.querySelector(selector);
        if (!target) return;
        target.scrollIntoView({block:'start', behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
      });
      nav.appendChild(button);
    }
  }

  function prepareView(view) {
    const profile = layouts[view.id];
    if (!profile) return;
    view.classList.add('layout-view');
    view.dataset.layout = profile.kind;
    view.querySelectorAll('.table-wrap').forEach(node => accessibleScroll(node, profile.kind === 'market' ? '完整行情表格，可横向滚动；交易对固定在左侧' : '完整数据表格，可横向滚动'));
    if (view.id === 'funding-trend') view.querySelectorAll('.funding-trend-grid').forEach(groupFundingMetrics);
    if (view.id === 'gainers-losers') prepareRankingScroll(view);
    if (view.id === 'onchain-opportunities') foldReadingGuide(view.querySelector('.onchain-method'), '机会分计算方法与数据说明');
    if (['spot-futures','dual-futures'].includes(view.id)) foldReadingGuide(view.querySelector('.strategy-grid'), '执行逻辑与数据说明');
    if (view.id === 'thinking') foldReadingGuide(view.querySelector('.strategy-grid'), '筛选原则与执行纪律');
    prepareOutline(view, profile);
  }

  let frame = 0;
  function scheduleActiveLayout() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const view = document.querySelector('main .view.active');
      if (view) prepareView(view);
    });
  }
  function resetNavigationScroll() {
    // Hash anchors otherwise jump below the shared title and freshness panel.
    // Only navigation resets the page; data refreshes keep the user's position.
    requestAnimationFrame(() => window.scrollTo({top:0, left:0, behavior:'instant'}));
  }
  document.body.classList.add('workspace-layout');
  document.querySelectorAll('main .view').forEach(prepareView);
  document.addEventListener('click', event => {
    if (event.target.closest?.('aside a[data-view]')) {
      scheduleActiveLayout();
      resetNavigationScroll();
    }
  });
  window.addEventListener('hashchange', () => {
    scheduleActiveLayout();
    if (layouts[location.hash.slice(1)]) resetNavigationScroll();
  });
  const main = document.querySelector('main');
  if (main) new MutationObserver(records => {
    if (records.some(record => record.target.closest?.('.view.active'))) scheduleActiveLayout();
  }).observe(main, {childList:true, subtree:true});
})();
