/* Bandwidth chart: two series (download to clients, upload from clients) over
   time, drawn as inline SVG. No charting library - the whole panel has to work
   with no internet access, which is exactly the situation it exists for.

   Colours come from CSS custom properties, so light and dark mode are handled
   by the stylesheet rather than duplicated here. */

(function () {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';

  function formatRate(bytesPerSecond) {
    const units = ['B/s', 'kB/s', 'MB/s', 'GB/s'];
    let value = bytesPerSecond;
    let index = 0;
    while (value >= 1000 && index < units.length - 1) {
      value /= 1000;
      index += 1;
    }
    const digits = value >= 100 || index === 0 ? 0 : 1;
    return value.toFixed(digits) + ' ' + units[index];
  }

  function formatTime(ts, window) {
    const date = new Date(ts * 1000);
    if (window === '7d' || window === '30d') {
      return date.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit' });
    }
    return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }

  /* A "nice" axis maximum, so gridlines land on readable numbers. */
  function niceMax(value) {
    if (value <= 0) return 1024;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    const normalised = value / magnitude;
    const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function el(name, attrs) {
    const node = document.createElementNS(NS, name);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }

  class BandwidthChart {
    constructor(root) {
      this.root = root;
      this.window = root.dataset.window || '10m';
      this.peerId = root.dataset.peer || '0';
      this.points = [];

      this.wrap = document.createElement('div');
      this.wrap.className = 'chart-wrap';

      this.legend = document.createElement('div');
      this.legend.className = 'chart-legend';
      this.legend.innerHTML =
        '<span class="legend-item"><span class="legend-swatch" style="background:var(--series-down)"></span>' +
        'Download <span class="legend-value" data-legend="down">–</span></span>' +
        '<span class="legend-item"><span class="legend-swatch" style="background:var(--series-up)"></span>' +
        'Upload <span class="legend-value" data-legend="up">–</span></span>' +
        '<span class="legend-item muted" data-legend="hint">aktuelle Rate</span>';

      this.svg = el('svg', { class: 'chart', preserveAspectRatio: 'none' });
      this.tooltip = document.createElement('div');
      this.tooltip.className = 'tooltip';

      this.wrap.appendChild(this.svg);
      this.wrap.appendChild(this.tooltip);
      root.appendChild(this.legend);
      root.appendChild(this.wrap);

      this.svg.addEventListener('pointermove', (event) => this.onMove(event));
      this.svg.addEventListener('pointerleave', () => this.hideTooltip());
      window.addEventListener('resize', () => this.draw());
    }

    async refresh() {
      try {
        const response = await fetch(
          `/api/series?peer_id=${encodeURIComponent(this.peerId)}&window=${encodeURIComponent(this.window)}`,
          { credentials: 'same-origin' }
        );
        if (!response.ok) return;
        const payload = await response.json();
        this.points = payload.points || [];
        this.draw();
      } catch (error) {
        /* A failed poll is not worth interrupting the page for; the next one
           will most likely succeed. */
      }
    }

    draw() {
      const width = this.wrap.clientWidth || 640;
      const height = 220;
      const pad = { top: 12, right: 8, bottom: 22, left: 54 };
      const plotWidth = Math.max(10, width - pad.left - pad.right);
      const plotHeight = height - pad.top - pad.bottom;

      this.svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      this.svg.innerHTML = '';

      const points = this.points;
      if (!points.length) return;

      const peak = Math.max(...points.map((p) => Math.max(p.down, p.up)), 0);
      const max = niceMax(peak * 1.15);

      const x = (i) => pad.left + (plotWidth * i) / Math.max(1, points.length - 1);
      const y = (value) => pad.top + plotHeight - (plotHeight * value) / max;

      /* Recessive grid: four lines, labelled on the left. */
      for (let step = 0; step <= 4; step += 1) {
        const value = (max * step) / 4;
        const yy = y(value);
        this.svg.appendChild(el('line', {
          class: 'grid-line', x1: pad.left, x2: width - pad.right, y1: yy, y2: yy,
        }));
        const label = el('text', { class: 'axis-label', x: pad.left - 8, y: yy + 4, 'text-anchor': 'end' });
        label.textContent = step === 0 ? '0' : formatRate(value);
        this.svg.appendChild(label);
      }

      const ticks = Math.min(5, points.length);
      for (let step = 0; step < ticks; step += 1) {
        const index = Math.round((step * (points.length - 1)) / Math.max(1, ticks - 1));
        const label = el('text', {
          class: 'axis-label', x: x(index), y: height - 6,
          'text-anchor': step === 0 ? 'start' : step === ticks - 1 ? 'end' : 'middle',
        });
        label.textContent = formatTime(points[index].ts, this.window);
        this.svg.appendChild(label);
      }

      const series = [
        { key: 'down', color: 'var(--series-down)' },
        { key: 'up', color: 'var(--series-up)' },
      ];

      for (const entry of series) {
        let line = '';
        let area = `M ${x(0)} ${y(0)}`;
        points.forEach((point, index) => {
          const px = x(index);
          const py = y(point[entry.key]);
          line += (index === 0 ? 'M' : 'L') + ` ${px} ${py} `;
          area += ` L ${px} ${py}`;
        });
        area += ` L ${x(points.length - 1)} ${y(0)} Z`;

        this.svg.appendChild(el('path', { d: area, fill: entry.color, 'fill-opacity': '0.16', stroke: 'none' }));
        this.svg.appendChild(el('path', {
          d: line, fill: 'none', stroke: entry.color, 'stroke-width': '2',
          'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        }));
      }

      this.crosshair = el('line', { class: 'crosshair', y1: pad.top, y2: pad.top + plotHeight, x1: -10, x2: -10 });
      this.svg.appendChild(this.crosshair);
      this.markers = series.map((entry) => {
        const marker = el('circle', {
          r: 4, fill: entry.color, stroke: 'var(--surface)', 'stroke-width': '2',
          cx: -10, cy: -10, opacity: '0',
        });
        this.svg.appendChild(marker);
        return marker;
      });

      this.geometry = { x, y, pad, plotWidth, plotHeight, width };

      const latest = points[points.length - 1];
      this.setLegend(latest, 'aktuelle Rate');
    }

    setLegend(point, hint) {
      const down = this.legend.querySelector('[data-legend="down"]');
      const up = this.legend.querySelector('[data-legend="up"]');
      const note = this.legend.querySelector('[data-legend="hint"]');
      if (down) down.textContent = formatRate(point.down);
      if (up) up.textContent = formatRate(point.up);
      if (note) note.textContent = hint;
    }

    onMove(event) {
      if (!this.geometry || !this.points.length) return;
      const rect = this.svg.getBoundingClientRect();
      const scale = this.geometry.width / rect.width;
      const px = (event.clientX - rect.left) * scale;

      /* The crosshair snaps to the nearest sample, so the reader aims at a
         moment in time rather than at a two-pixel line. */
      const ratio = (px - this.geometry.pad.left) / this.geometry.plotWidth;
      let index = Math.round(ratio * (this.points.length - 1));
      index = Math.max(0, Math.min(this.points.length - 1, index));
      const point = this.points[index];

      const cx = this.geometry.x(index);
      this.crosshair.setAttribute('x1', cx);
      this.crosshair.setAttribute('x2', cx);
      this.markers[0].setAttribute('cx', cx);
      this.markers[0].setAttribute('cy', this.geometry.y(point.down));
      this.markers[0].setAttribute('opacity', '1');
      this.markers[1].setAttribute('cx', cx);
      this.markers[1].setAttribute('cy', this.geometry.y(point.up));
      this.markers[1].setAttribute('opacity', '1');

      this.tooltip.textContent = '';
      const time = document.createElement('div');
      time.className = 'tt-time';
      time.textContent = new Date(point.ts * 1000).toLocaleString();
      this.tooltip.appendChild(time);

      for (const entry of [
        { label: 'Download', value: point.down, color: 'var(--series-down)' },
        { label: 'Upload', value: point.up, color: 'var(--series-up)' },
      ]) {
        const row = document.createElement('div');
        row.className = 'tt-row';
        const swatch = document.createElement('span');
        swatch.className = 'legend-swatch';
        swatch.style.background = entry.color;
        const name = document.createElement('span');
        name.textContent = entry.label;
        const value = document.createElement('span');
        value.className = 'tt-value';
        value.textContent = formatRate(entry.value);
        row.append(swatch, name, value);
        this.tooltip.appendChild(row);
      }

      const left = Math.min(
        Math.max(0, (cx / scale) - this.tooltip.offsetWidth / 2),
        rect.width - this.tooltip.offsetWidth
      );
      this.tooltip.style.left = `${left}px`;
      this.tooltip.style.top = '4px';
      this.tooltip.style.opacity = '1';
    }

    hideTooltip() {
      this.tooltip.style.opacity = '0';
      if (this.crosshair) {
        this.crosshair.setAttribute('x1', -10);
        this.crosshair.setAttribute('x2', -10);
      }
      if (this.markers) this.markers.forEach((marker) => marker.setAttribute('opacity', '0'));
      if (this.points.length) this.setLegend(this.points[this.points.length - 1], 'aktuelle Rate');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-chart="bandwidth"]').forEach((root) => {
      const chart = new BandwidthChart(root);
      chart.refresh();
      setInterval(() => chart.refresh(), root.dataset.window === '10m' ? 5000 : 30000);
    });
  });
})();
