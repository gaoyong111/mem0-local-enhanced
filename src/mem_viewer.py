"""mem0 记忆可视化 Web UI — Flask + vis.js Network 图谱驱动"""

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

# mem0 安装目录
_MEM0_DIR = os.getenv('MEM0_DIR', os.path.expanduser('~/.mem0'))
if _MEM0_DIR not in sys.path:
    sys.path.insert(0, _MEM0_DIR)

from hybrid_search import (  # noqa: E402
    CHROMA_DB_PATH,
    HISTORY_DB,
    detect_project,
    extract_keywords,
    hybrid_search,
    normalize_project,
)
from mem0_add_policy import (  # noqa: E402
    CATEGORY_COLORS,
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    apply_category_metadata,
    normalize_category,
)
from memory_lineage import build_timeline, record_event  # noqa: E402

# 配置
HOST = 'localhost'
PORT = 8765
DEFAULT_USER = os.getenv('MEM0_USER_ID', 'default-user')
# 与 mcp_server_local.py DEFAULT_MAX_RESULTS 保持一致，便于对比检索效果
MCP_SEARCH_MAX_RESULTS = 8

# 项目颜色映射（固定 8 色，超出后循环）
_PROJECT_COLORS = [
    '#e74c3c', '#3498db', '#2ecc71', '#f39c12',
    '#9b59b6', '#1abc9c', '#e67e22', '#34495e',
]
_GLOBAL_COLOR = '#95a5a6'


def load_all_memories() -> list[dict]:
    """从 active_memories + Chroma 加载全部活跃记忆。"""
    from memory_sync import load_active_memories, load_active_metadata, migrate_active_if_needed

    migrate_active_if_needed()
    text_map = load_active_memories()
    metadata_map = load_active_metadata()

    created_at_map: dict[str, str] = {}
    conn = sqlite3.connect(os.path.expanduser('~/.mem0/active_memories.db'))
    try:
        for memory_id, created_at, updated_at in conn.execute(
            'SELECT memory_id, created_at, updated_at FROM active_memories'
        ):
            created_at_map[memory_id] = created_at or updated_at or ''
    finally:
        conn.close()

    update_count_map: dict[str, int] = {}
    conn = sqlite3.connect(HISTORY_DB)
    try:
        update_rows = conn.execute(
            "SELECT memory_id, count(*) FROM history WHERE event = 'UPDATE' AND is_deleted = 0 GROUP BY memory_id"
        ).fetchall()
        update_count_map = {row[0]: row[1] for row in update_rows if row[0]}
    finally:
        conn.close()

    memories = []
    for memory_id, text in text_map.items():
        meta = metadata_map.get(memory_id, {})
        # 归一化 project="全局" → ""，统一灰色显示
        raw_project = meta.get('project', '')
        project = '' if raw_project == '全局' else raw_project
        raw_category = str(meta.get('category', '') or '')
        normalized_category = normalize_category(raw_category)
        memories.append({
            'id': memory_id,
            'text': text,
            'project': project,
            'category': normalized_category,
            'category_raw': raw_category,
            'category_label': CATEGORY_LABELS.get(normalized_category, normalized_category),
            'metadata': meta,
            'created_at': created_at_map.get(memory_id, ''),
            'update_count': update_count_map.get(memory_id, 0),
        })

    return memories


def _load_chroma_metadata() -> dict[str, dict]:
    """从 ChromaDB 加载 memory_id -> metadata 映射。"""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        result = col.get(include=['metadatas'])
    except Exception:
        return {}

    metadata_map: dict[str, dict] = {}
    ids = result.get('ids') or []
    metas = result.get('metadatas') or []
    for memory_id, meta in zip(ids, metas):
        if not meta:
            continue
        metadata_map[memory_id] = {
            'project': str(meta.get('project', '') or ''),
            'category': str(meta.get('category', '') or ''),
            'storage_mode': str(meta.get('storage_mode', '') or ''),
            'module': str(meta.get('module', '') or ''),
            'field': str(meta.get('field', '') or ''),
            'keywords': str(meta.get('keywords', '') or ''),
        }
    return metadata_map


def assign_project_colors(memories: list[dict]) -> dict[str, str]:
    """为每个 project 分配颜色。"""
    projects = sorted(set(m['project'] for m in memories if m['project']))
    color_map = {}
    for index, project in enumerate(projects):
        color_map[project] = _PROJECT_COLORS[index % len(_PROJECT_COLORS)]
    return color_map


def compute_thickness(memories: list[dict], edges: list[dict]) -> dict[str, dict]:
    """计算每条记忆的三种厚度：变更厚度(update_count)、连接厚度(edge_count)、重复厚度(repetition_count)。
    返回 {memory_id: {change, connection, repetition, shape, size, borderWidth}}。"""
    # 变更厚度（直接从 update_count 字段取）
    # 连接厚度（统计每个节点的边数）
    edge_count: dict[str, int] = defaultdict(int)
    for edge in edges:
        edge_count[edge['from']] += 1
        edge_count[edge['to']] += 1

    # 重复厚度：统计每条记忆与其他记忆的关键词重叠≥30%的数量
    word_sets = {}
    for m in memories:
        words = set(extract_keywords(m['text']))
        keywords_meta = m['metadata'].get('keywords', '')
        if keywords_meta:
            words.update(keywords_meta.lower().split(','))
        word_sets[m['id']] = words

    repetition_count: dict[str, int] = defaultdict(int)
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            id_a, id_b = memories[i]['id'], memories[j]['id']
            words_a, words_b = word_sets[id_a], word_sets[id_b]
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b)
            min_len = min(len(words_a), len(words_b))
            ratio = overlap / min_len if min_len else 0
            if ratio >= 0.3:
                repetition_count[id_a] += 1
                repetition_count[id_b] += 1

    # 映射到视觉属性
    thickness_map: dict[str, dict] = {}
    for m in memories:
        update_count_val = m.get('update_count', 0)
        conn_count = edge_count.get(m['id'], 0)
        rep_count = repetition_count.get(m['id'], 0)

        # 变更厚度 → 形状：0次=dot, 1次=diamond, 2+次=star
        if update_count_val >= 2:
            shape = 'star'
        elif update_count_val == 1:
            shape = 'diamond'
        else:
            shape = 'dot'

        # 重复厚度 → 大小：基础15，每个重复+3，上限40
        size = min(15 + rep_count * 3, 40)

        # 光晕：重复≥3时启用 shadow
        shadow = rep_count >= 3

        thickness_map[m['id']] = {
            'change': update_count_val,
            'connection': conn_count,
            'repetition': rep_count,
            'shape': shape,
            'size': size,
            'shadow': shadow,
        }

    return thickness_map


from datetime import datetime, timedelta  # noqa: E402


def compute_edges(memories: list[dict]) -> list[dict]:
    """计算图谱边：关键词重叠 ≥40%（内容关联）+ 24小时内创建（时间邻近）。
    内容边用实线蓝色，时间边用虚线灰色。"""
    edges = []
    edge_index: dict[str, dict] = {}

    def add_edge(source_id: str, target_id: str, edge_type: str, weight: int):
        key = f'{source_id}-{target_id}' if source_id < target_id else f'{target_id}-{source_id}'
        if key not in edge_index:
            edge_index[key] = {'from': source_id, 'to': target_id, 'type': edge_type, 'weight': weight}
        elif edge_type == 'keyword':
            # 关键词边优先，覆盖时间边
            edge_index[key]['type'] = 'keyword'
            edge_index[key]['weight'] = weight

    # 关键词重叠
    word_sets = {}
    for m in memories:
        words = set(extract_keywords(m['text']))
        keywords_meta = m['metadata'].get('keywords', '')
        if keywords_meta:
            words.update(keywords_meta.lower().split(','))
        word_sets[m['id']] = words

    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            id_a, id_b = memories[i]['id'], memories[j]['id']
            words_a, words_b = word_sets[id_a], word_sets[id_b]
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b)
            min_len = min(len(words_a), len(words_b))
            ratio = overlap / min_len if min_len else 0
            if ratio >= 0.4:
                add_edge(id_a, id_b, 'keyword', overlap)

    # 时间链：按创建时间排序，每条记忆只连到紧邻的前一条（同项目内）
    # 形成时间流，像串珠子，不是毛线团
    project_sorted = defaultdict(list)
    for m in memories:
        created = m.get('created_at', '')
        if created and m['project']:
            try:
                ts = datetime.fromisoformat(created.replace('Z', '+00:00'))
                project_sorted[m['project']].append((ts, m['id']))
            except (ValueError, TypeError):
                pass

    for project, items in project_sorted.items():
        items.sort(key=lambda x: x[0])
        for idx in range(1, len(items)):
            key = f'{items[idx-1][1]}-{items[idx][1]}' if items[idx-1][1] < items[idx][1] else f'{items[idx][1]}-{items[idx-1][1]}'
            if key not in edge_index:
                edge_index[key] = {'from': items[idx-1][1], 'to': items[idx][1], 'type': 'time', 'weight': 1}

    for edge_data in edge_index.values():
        is_keyword = edge_data['type'] == 'keyword'
        edges.append({
            'from': edge_data['from'],
            'to': edge_data['to'],
            'width': min(edge_data['weight'], 5) if is_keyword else 1,
            'color': {'color': '#3498db' if is_keyword else '#2c3e50', 'highlight': '#3498db'},
            'dashes': not is_keyword,
            'edgeType': edge_data['type'],
        })

    return edges


from flask import Flask, jsonify, render_template_string, request  # noqa: E402

app = Flask(__name__)

VIS_JS_CDN = 'https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js'

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>mem0 记忆图谱</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: -apple-system, 'PingFang SC', sans-serif; background:#1a1a2e; color:#e0e0e0; display:flex; flex-direction:column; height:100vh; }

  .toolbar { display:flex; align-items:center; gap:12px; padding:12px 16px; background:#16213e; border-bottom:1px solid #0f3460; }
  .toolbar input[type=text] { flex:1; min-width:120px; padding:8px 12px; border-radius:6px; border:1px solid #0f3460; background:#1a1a2e; color:#e0e0e0; font-size:14px; }
  .toolbar input[type=text]:focus { outline:none; border-color:#3498db; }
  .toolbar select { padding:8px 12px; border-radius:6px; border:1px solid #0f3460; background:#1a1a2e; color:#e0e0e0; font-size:14px; }
  .toolbar button { padding:8px 16px; border-radius:6px; border:none; cursor:pointer; font-size:14px; transition:opacity .2s; }
  .toolbar button:hover { opacity:0.8; }
  .btn-search { background:#3498db; color:#fff; }
  .btn-reset { background:#e74c3c; color:#fff; }

  .search-results { padding:8px 16px; background:#16213e; border-bottom:1px solid #0f3460; max-height:220px; overflow-y:auto; display:none; }
  .search-results.visible { display:block; }
  .search-results-head { font-size:12px; color:#7f8c8d; margin-bottom:8px; }
  .search-result-item { display:flex; gap:10px; align-items:flex-start; padding:8px 10px; margin-bottom:6px; border-radius:6px; background:#1a1a2e; cursor:pointer; border:1px solid transparent; }
  .search-result-item:hover { border-color:#3498db; }
  .search-result-item.dimmed { opacity:0.45; }
  .search-result-rank { font-size:12px; color:#3498db; min-width:28px; font-weight:600; }
  .search-result-score { font-size:12px; color:#f39c12; min-width:110px; white-space:nowrap; }
  .search-result-text { font-size:13px; color:#e0e0e0; line-height:1.5; flex:1; }
  .search-result-meta { font-size:11px; color:#95a5a6; margin-top:4px; }

  .main { display:flex; flex:1; overflow:hidden; }
  #graph-container { flex:1; }
  #detail-panel { width:320px; padding:16px; background:#16213e; border-left:1px solid #0f3460; overflow-y:auto; display:none; }
  #detail-panel.visible { display:block; }

  .detail-title { font-size:16px; font-weight:600; margin-bottom:12px; color:#3498db; }
  .detail-section { margin-bottom:16px; }
  .detail-label { font-size:12px; color:#7f8c8d; margin-bottom:4px; }
  .detail-text { font-size:14px; line-height:1.6; white-space:pre-wrap; }
  .detail-meta { font-size:12px; color:#95a5a6; }
  .timeline-list { list-style:none; padding:0; margin:8px 0 0; }
  .timeline-item { border-left:2px solid #3498db; padding:6px 0 6px 10px; margin-bottom:8px; }
  .timeline-item .tl-head { font-size:12px; color:#3498db; margin-bottom:4px; }
  .timeline-item .tl-body { font-size:12px; color:#bdc3c7; line-height:1.5; white-space:pre-wrap; }
  .timeline-link { color:#f39c12; cursor:pointer; text-decoration:underline; }
  .detail-edit-row { display:flex; flex-direction:column; gap:8px; margin-bottom:8px; }
  .detail-edit-row input, .detail-edit-row select { width:100%; padding:6px 10px; border-radius:6px; border:1px solid #0f3460; background:#1a1a2e; color:#e0e0e0; font-size:13px; }
  .detail-edit-row input:focus, .detail-edit-row select:focus { outline:none; border-color:#3498db; }
  .btn-save { margin-top:4px; padding:6px 14px; background:#2ecc71; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px; }
  .btn-save:hover { opacity:0.8; }
  .btn-save:disabled { opacity:0.5; cursor:not-allowed; }
  .btn-delete { margin-top:16px; padding:8px 16px; background:#e74c3c; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px; }
  .btn-delete:hover { opacity:0.8; }

  .stats { padding:8px 16px; background:#16213e; border-top:1px solid #0f3460; font-size:12px; color:#7f8c8d; text-align:center; }
  .legend { display:inline; margin-left:16px; }
  .legend span { margin-right:8px; }

  .legend-panel { position:absolute; bottom:40px; left:16px; padding:12px 16px; background:#16213e; border:1px solid #0f3460; border-radius:8px; font-size:12px; color:#e0e0e0; z-index:10; max-width:280px; }
  .legend-panel h4 { margin:0 0 8px; font-size:13px; color:#3498db; }
  .legend-item { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
  .legend-dot { width:14px; height:14px; border-radius:50%; flex-shrink:0; }
  .legend-shape { width:12px; height:12px; flex-shrink:0; background:#e0e0e0; }
  .legend-shape.dot { border-radius:50%; }
  .legend-shape.diamond { border-radius:2px; transform:rotate(45deg) scale(0.7); }
  .legend-shape.star { clip-path:polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%); }
</style>
</head>
<body>

<div class="toolbar">
  <input type="text" id="search-input" placeholder="搜索记忆..." />
  <select id="project-filter">
    <option value="">全部项目</option>
    {% for project in projects %}
    <option value="{{ project }}">{{ project }}</option>
    {% endfor %}
  </select>
  <select id="category-filter">
    <option value="">全部分类</option>
    {% for cat, label in category_items %}
    <option value="{{ cat }}">{{ label }}</option>
    {% endfor %}
  </select>
  <button class="btn-search" onclick="doSearch()">搜索</button>
  <button class="btn-reset" onclick="resetGraph()">重置</button>
</div>

<div id="search-results" class="search-results">
  <div class="search-results-head" id="search-results-head"></div>
  <div id="search-results-list"></div>
</div>

<div class="legend-panel">
  <h4>分类颜色</h4>
  {% for cat, label in category_items %}
  <div class="legend-item">
    <div class="legend-dot" style="background:{{ category_colors[cat] }}"></div>
    <span>{{ label }}</span>
  </div>
  {% endfor %}
  <hr style="border-color:#0f3460;margin:8px 0" />
  <h4>节点形状</h4>
  <div class="legend-item">
    <div class="legend-shape dot"></div>
    <span>无变更</span>
  </div>
  <div class="legend-item">
    <div class="legend-shape diamond"></div>
    <span>1次更新</span>
  </div>
  <div class="legend-item">
    <div class="legend-shape star"></div>
    <span>2+次更新</span>
  </div>
  <hr style="border-color:#0f3460;margin:8px 0" />
  <div class="legend-item">
    <span style="font-size:16px">⬤</span> vs <span style="font-size:10px">●</span>
    <span>节点大小 = 重复提及</span>
  </div>
  <div class="legend-item">
    <div class="legend-dot" style="background:#95a5a6;box-shadow:0 0 8px #e0e0e0"></div>
    <span>光晕 = 重复≥3次</span>
  </div>
</div>

<div class="main">
  <div id="graph-container"></div>
  <div id="detail-panel">
    <div class="detail-title" id="detail-title"></div>
    <div class="detail-section">
      <div class="detail-label">记忆正文</div>
      <div class="detail-text" id="detail-text"></div>
    </div>
    <div class="detail-section">
      <div class="detail-label">项目 / 分类</div>
      <div class="detail-edit-row">
        <input type="text" id="edit-project" placeholder="留空 = 全局" />
        <select id="edit-category">
          {% for cat, label in category_items %}
          <option value="{{ cat }}">{{ label }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="detail-meta" id="detail-created"></div>
      <button class="btn-save" id="btn-save" onclick="saveMetadata()">保存</button>
    </div>
    <div class="detail-section">
      <div class="detail-label">演变时间线</div>
      <div class="detail-meta" id="detail-ancestors"></div>
      <ul class="timeline-list" id="detail-timeline"></ul>
    </div>
    <div class="detail-section">
      <div class="detail-label">Metadata</div>
      <div class="detail-meta" id="detail-meta"></div>
    </div>
    <div class="detail-section">
      <div class="detail-label">厚度指标</div>
      <div class="detail-meta" id="detail-thickness"></div>
    </div>
    <div class="detail-section">
      <div class="detail-label">ID</div>
      <div class="detail-meta" id="detail-id"></div>
    </div>
    <button class="btn-delete" id="btn-delete" onclick="deleteMemory()">删除此记忆</button>
  </div>
</div>

<div class="stats" id="stats">
  共 <span id="total-count">0</span> 条记忆
  <span class="legend">●无变更 ◆1次更新 ★2+次更新 | 大=重复提及多 | 光晕=重复≥3</span>
</div>

<script src="{{ vis_js_cdn }}"></script>
<script>
  const nodesData = {{ nodes_json | safe }};
  const edgesData = {{ edges_json | safe }};
  const memoriesMap = {{ memories_map_json | safe }};
  const thicknessMap = {{ thickness_json | safe }};
  let selectedId = null;
  const deletedNodeIds = new Set();
  let lastSearchResults = [];
  let lastSearchPayload = null;
  const MCP_SEARCH_MAX_RESULTS = {{ mcp_search_max_results }};

  const nodes = new vis.DataSet(nodesData);
  const edges = new vis.DataSet(edgesData);
  // 保存节点原始颜色，用于重置还原
  const originalNodeColors = {};
  nodesData.forEach(n => { originalNodeColors[n.id] = n.color; });
  const container = document.getElementById('graph-container');
  const options = {
    nodes: { shape: 'dot', font: { size: 11, color: '#e0e0e0' }, borderWidth: 1, borderWidthSelected: 3 },
    edges: { color: { color: '#0f3460', highlight: '#3498db', hover: '#2980b9' }, width: 1, smooth: { type: 'continuous' } },
    physics: { barnesHut: { gravitationalConstant: -2000, centralGravity: 0.3, springLength: 100, springConstant: 0.04 }, stabilization: { iterations: 100 } },
    interaction: { hover: true, tooltipDelay: 200, navigationButtons: true, keyboard: true },
  };
  const network = new vis.Network(container, { nodes, edges }, options);

  network.on('click', function(params) {
    if (params.nodes.length > 0) {
      selectedId = params.nodes[0];
      showDetail(selectedId);
    } else {
      hideDetail();
    }
  });

  function showDetail(id) {
    const mem = memoriesMap[id];
    if (!mem) return;
    const thick = thicknessMap[id] || {};
    document.getElementById('detail-title').textContent = mem.project ? '[' + mem.project + ']' : '[全局]';
    document.getElementById('detail-text').textContent = mem.text;
    document.getElementById('edit-project').value = mem.project || '';
    document.getElementById('edit-category').value = mem.category || 'episodic';
    document.getElementById('detail-created').textContent =
      '创建: ' + (mem.created_at || '未知') +
      (mem.category_raw && mem.category_raw !== mem.category ? ' | 原分类: ' + mem.category_raw : '');
    document.getElementById('detail-meta').textContent = JSON.stringify(mem.metadata, null, 2);
    const changeEmoji = thick.change >= 2 ? '★' : thick.change === 1 ? '◆' : '●';
    document.getElementById('detail-thickness').textContent =
      changeEmoji + ' 变更:' + thick.change + '  |  连接:' + thick.connection + '  |  重复:' + thick.repetition;
    document.getElementById('detail-id').textContent = id;
    document.getElementById('detail-panel').classList.add('visible');
    loadTimeline(id);
  }

  function actionLabel(action) {
    const labels = {
      ADD: '创建',
      UPDATE: '内容修正',
      DELETE: '删除',
      MERGE: '合并',
      DEDUP_DROP: '去重删除',
      CATEGORY_CHANGE: '分类变更',
      GROOMING: '梳理',
    };
    return labels[action] || action;
  }

  function renderTimelineEvents(container, events) {
    container.innerHTML = '';
    if (!events || events.length === 0) {
      container.innerHTML = '<li class="timeline-item"><div class="tl-body">暂无演变记录</div></li>';
      return;
    }
    events.forEach(ev => {
      const li = document.createElement('li');
      li.className = 'timeline-item';
      const sources = (ev.source_ids || []).filter(Boolean);
      const sourceText = sources.length ? ('来源: ' + sources.join(', ')) : '';
      const targetText = ev.target_id ? ('保留: ' + ev.target_id) : '';
      li.innerHTML =
        '<div class="tl-head">' + (ev.ts || '未知时间') + ' · ' + actionLabel(ev.action) +
        (ev.origin ? ' (' + ev.origin + ')' : '') + '</div>' +
        '<div class="tl-body">' +
        (ev.note ? ev.note + '\\n' : '') +
        (sourceText ? sourceText + '\\n' : '') +
        (targetText ? targetText + '\\n' : '') +
        (ev.content_preview || '') +
        '</div>';
      container.appendChild(li);
    });
  }

  function loadTimeline(id) {
    const timelineEl = document.getElementById('detail-timeline');
    const ancestorsEl = document.getElementById('detail-ancestors');
    timelineEl.innerHTML = '<li class="timeline-item"><div class="tl-body">加载中...</div></li>';
    ancestorsEl.textContent = '';
    fetch('/api/timeline/' + encodeURIComponent(id))
      .then(r => r.json())
      .then(data => {
        renderTimelineEvents(timelineEl, data.events || []);
        const ancestors = data.ancestor_ids || [];
        if (ancestors.length) {
          ancestorsEl.innerHTML = '上游记忆: ' + ancestors.map(aid =>
            '<span class="timeline-link" onclick="jumpToMemory(\\'' + aid + '\\')">' + aid.slice(0, 8) + '...</span>'
          ).join(' · ');
        } else {
          ancestorsEl.textContent = '上游记忆: 无';
        }
      })
      .catch(err => {
        console.error('timeline failed', err);
        timelineEl.innerHTML = '<li class="timeline-item"><div class="tl-body">时间线加载失败</div></li>';
      });
  }

  function jumpToMemory(id) {
    if (!memoriesMap[id]) {
      alert('该上游记忆已不在当前图谱（可能已删除）');
      return;
    }
    selectedId = id;
    showDetail(id);
    network.selectNodes([id]);
    network.focus(id, { scale: 1.2, animation: true });
  }

  function hideDetail() {
    selectedId = null;
    document.getElementById('detail-panel').classList.remove('visible');
  }

  function getFilters() {
    return {
      project: document.getElementById('project-filter').value,
      category: document.getElementById('category-filter').value,
    };
  }

  function passesFilters(mem, filters) {
    if (!mem) return false;
    if (filters.project && mem.project !== filters.project) return false;
    if (filters.category && mem.category !== filters.category) return false;
    return true;
  }

  function renderSearchResults(payload) {
    const panel = document.getElementById('search-results');
    const head = document.getElementById('search-results-head');
    const list = document.getElementById('search-results-list');
    lastSearchResults = payload.results || [];
    lastSearchPayload = payload;
    if (!lastSearchResults.length) {
      panel.classList.remove('visible');
      list.innerHTML = '';
      return;
    }

    const filters = getFilters();
    const scope = payload.effective_project
      ? ('项目作用域: ' + payload.effective_project)
      : '项目作用域: 全局（未 detect 到项目，与 MCP project=\"\" 且 cwd 无项目时一致）';
    head.textContent =
      '混合检索 Top ' + lastSearchResults.length +
      '（MCP 同算法 hybrid_search，max=' + payload.max_results + '）· ' + scope +
      ' · 比对 MCP 请用相同 query + project';

    list.innerHTML = '';
    lastSearchResults.forEach((item, index) => {
      const mem = memoriesMap[item.id];
      const filteredOut = mem && !passesFilters(mem, filters);
      const row = document.createElement('div');
      row.className = 'search-result-item' + (filteredOut ? ' dimmed' : '');
      const scopeTag = item.project ? ('[' + item.project + ']') : '[全局]';
      row.innerHTML =
        '<div class="search-result-rank">#' + (index + 1) + '</div>' +
        '<div class="search-result-score">score=' + Number(item.score).toFixed(2) + '<br>(' + (item.source || 'unknown') + ')</div>' +
        '<div class="search-result-text">' +
          item.text +
          '<div class="search-result-meta">' + scopeTag + ' · ' + item.id +
          (filteredOut ? ' · 已被项目/分类筛选隐藏' : '') +
          '</div>' +
        '</div>';
      row.onclick = () => focusSearchResult(item.id);
      list.appendChild(row);
    });
    panel.classList.add('visible');
  }

  function focusSearchResult(id) {
    if (!memoriesMap[id] || deletedNodeIds.has(id)) {
      alert('该结果不在当前图谱中（可能已删除）');
      return;
    }
    selectedId = id;
    showDetail(id);
    network.selectNodes([id]);
    network.focus(id, { scale: 1.2, animation: true });
  }

  function applyGraphVisibility(matchSet, searchResults) {
    const filters = getFilters();
    const maxScore = (searchResults && searchResults.length)
      ? Math.max(...searchResults.map(item => Number(item.score) || 0), 1)
      : 1;

    nodes.update(nodesData.filter(n => !deletedNodeIds.has(n.id)).map(n => {
      const mem = memoriesMap[n.id];
      const passes = passesFilters(mem, filters);
      const inSearch = matchSet ? matchSet.has(n.id) : true;
      const result = (searchResults || []).find(item => item.id === n.id);
      const scoreRatio = result ? (Number(result.score) || 0) / maxScore : 0.15;
      const dimmed = matchSet ? !matchSet.has(n.id) : false;
      return {
        ...n,
        hidden: matchSet ? (!passes || !inSearch) : !passes,
        opacity: !passes ? 0 : (matchSet ? (dimmed ? 0.12 : Math.max(0.35, scoreRatio)) : 1.0),
        borderWidth: result ? 2 : 1,
      };
    }));

    if (matchSet) {
      const edgeIds = edges.getIds();
      const edgeUpdates = edgeIds.map(eid => {
        const edge = edges.get(eid);
        const bothMatch = matchSet.has(edge.from) && matchSet.has(edge.to);
        return {
          id: eid,
          color: bothMatch ? { color: '#3498db', highlight: '#3498db' } : { color: '#1a1a2e', highlight: '#1a1a2e' },
          opacity: bothMatch ? 1.0 : 0.08,
        };
      });
      edges.update(edgeUpdates);
    }
  }

  function doSearch() {
    const query = document.getElementById('search-input').value.trim();
    if (!query) {
      document.getElementById('search-results').classList.remove('visible');
      applyGraphVisibility(null, []);
      updateStats();
      return;
    }
    const projectFilter = document.getElementById('project-filter').value;
    let url = '/search?q=' + encodeURIComponent(query);
    if (projectFilter) {
      url += '&project=' + encodeURIComponent(projectFilter);
    } else {
      // 全部项目 = 全局检索（project 空），与 MCP 显式传 project="" 且 detect 不到项目时一致
      url += '&project=';
    }
    fetch(url)
      .then(r => r.json())
      .then(payload => {
        lastSearchPayload = payload;
        const results = payload.results || [];
        const matchSet = new Set(results.map(item => item.id));
        renderSearchResults(payload);
        applyGraphVisibility(matchSet, results);
        const visibleCount = nodes.get().filter(n => !n.hidden).length;
        document.getElementById('stats').innerHTML =
          '检索 ' + results.length + ' 条 | 图谱可见 ' + visibleCount + ' 节点' +
          ' <span class="legend">边框加粗=命中项 | 亮度≈score</span>';
      })
      .catch(err => {
        console.error('Search failed:', err);
        document.getElementById('stats').textContent = '搜索失败，请重试';
      });
  }

  function onFilterChange() {
    const query = document.getElementById('search-input').value.trim();
    if (query) {
      if (lastSearchPayload && lastSearchResults.length) {
        renderSearchResults(lastSearchPayload);
        applyGraphVisibility(new Set(lastSearchResults.map(item => item.id)), lastSearchResults);
      } else {
        doSearch();
      }
      return;
    }
    applyGraphVisibility(null, []);
    updateStats();
  }

  document.getElementById('project-filter').addEventListener('change', onFilterChange);
  document.getElementById('category-filter').addEventListener('change', onFilterChange);

  document.getElementById('search-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doSearch();
  });

  function resetGraph() {
    document.getElementById('search-input').value = '';
    document.getElementById('project-filter').value = '';
    document.getElementById('category-filter').value = '';
    document.getElementById('search-results').classList.remove('visible');
    lastSearchResults = [];
    lastSearchPayload = null;
    nodes.update(nodesData.filter(n => !deletedNodeIds.has(n.id)).map(n => ({
      ...n,
      opacity: 1.0,
      hidden: false,
      borderWidth: 1,
    })));
    edges.update(edgesData.map(e => ({...e})));
    hideDetail();
    updateStats();
  }

  function saveMetadata() {
    if (!selectedId) return;
    const mem = memoriesMap[selectedId];
    if (!mem) return;

    const project = document.getElementById('edit-project').value.trim();
    const category = document.getElementById('edit-category').value;
    const payload = {};
    if (project !== (mem.project || '')) payload.project = project;
    if (category !== mem.category) payload.category = category;
    if (Object.keys(payload).length === 0) {
      alert('无变更');
      return;
    }

    const btn = document.getElementById('btn-save');
    btn.disabled = true;
    btn.textContent = '保存中...';

    fetch('/api/update/' + encodeURIComponent(selectedId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(r => r.json())
      .then(result => {
        btn.disabled = false;
        btn.textContent = '保存';
        if (!result.ok) {
          alert('保存失败: ' + (result.error || '未知错误'));
          return;
        }
        if (!result.changed) {
          alert('无变更');
          return;
        }

        mem.project = result.project || '';
        mem.category = result.category || mem.category;
        mem.category_label = result.category_label || mem.category_label;
        mem.category_raw = result.category_raw || '';
        if (mem.metadata) {
          mem.metadata.project = mem.project;
          mem.metadata.category = mem.category;
          if (mem.category_raw) {
            mem.metadata.category_raw = mem.category_raw;
          }
        }

        const nodeColor = {{ category_colors_json | safe }}[mem.category] || '{{ global_color }}';
        nodes.update({
          id: selectedId,
          color: nodeColor,
          title: (mem.project ? '[' + mem.project + '] ' : '[全局] ') +
            mem.text.slice(0, 60) + (mem.text.length > 60 ? '...' : ''),
        });
        originalNodeColors[selectedId] = nodeColor;

        showDetail(selectedId);
        loadTimeline(selectedId);
      })
      .catch(err => {
        btn.disabled = false;
        btn.textContent = '保存';
        console.error('save failed', err);
        alert('保存失败，请重试');
      });
  }

  function deleteMemory() {
    if (!selectedId) return;
    const reason = prompt('请填写删除原因（必填，便于追溯）：');
    if (reason === null) return;
    if (!reason.trim()) {
      alert('必须填写删除原因');
      return;
    }
    if (!confirm('确认删除记忆 ' + selectedId + '？\n原因：' + reason.trim())) return;
    fetch('/delete/' + selectedId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason.trim() }),
    })
      .then(r => r.json())
      .then(result => {
        if (result.ok) {
          deletedNodeIds.add(selectedId);
          nodes.remove(selectedId);
          const relatedEdges = edges.getIds().filter(eid => {
            const edge = edges.get(eid);
            return edge.from === selectedId || edge.to === selectedId;
          });
          edges.remove(relatedEdges);
          delete memoriesMap[selectedId];
          hideDetail();
          updateStats();
        } else {
          alert('删除失败: ' + result.error);
        }
      });
  }

  function updateStats() {
    const count = nodes.getIds().length;
    document.getElementById('total-count').textContent = count;
  }
  updateStats();
</script>
</body>
</html>'''


@app.route('/')
def index():
    """主页面：渲染图谱。"""
    memories = load_all_memories()
    edges = compute_edges(memories)
    thickness = compute_thickness(memories, edges)

    nodes_json = json.dumps([
        {
            'id': m['id'],
            'label': m['text'][:30] + ('...' if len(m['text']) > 30 else ''),
            'title': (f"[{m['project']}] " if m['project'] else '[全局] ') + m['text'][:60] + ('...' if len(m['text']) > 60 else ''),
            'color': CATEGORY_COLORS.get(m['category'], _GLOBAL_COLOR),
            'shape': thickness[m['id']]['shape'],
            'size': thickness[m['id']]['size'],
            'shadow': thickness[m['id']]['shadow'] if thickness[m['id']]['shadow'] else None,
        }
        for m in memories
    ], ensure_ascii=False)

    edges_json = json.dumps(edges, ensure_ascii=False)

    memories_map = {m['id']: m for m in memories}
    memories_map_json = json.dumps(memories_map, ensure_ascii=False)

    thickness_json = json.dumps(thickness, ensure_ascii=False)

    projects = sorted(set(m['project'] for m in memories if m['project']))
    category_items = [(cat, CATEGORY_LABELS[cat]) for cat in sorted(VALID_CATEGORIES)]
    category_colors_json = json.dumps(CATEGORY_COLORS, ensure_ascii=False)

    return render_template_string(
        HTML_TEMPLATE,
        nodes_json=nodes_json,
        edges_json=edges_json,
        memories_map_json=memories_map_json,
        thickness_json=thickness_json,
        projects=projects,
        category_items=category_items,
        category_colors=CATEGORY_COLORS,
        category_colors_json=category_colors_json,
        vis_js_cdn=VIS_JS_CDN,
        global_color=_GLOBAL_COLOR,
        mcp_search_max_results=MCP_SEARCH_MAX_RESULTS,
    )


@app.route('/api/timeline/<memory_id>')
def timeline(memory_id: str):
    """返回记忆的演变时间线（history.db + lineage.jsonl）及上游 ID。"""
    return jsonify(build_timeline(memory_id))


@app.route('/search')
def search():
    """搜索接口：与 MCP search_memory 相同 hybrid_search 逻辑，返回带 score 的结果。"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'results': [], 'effective_project': '', 'max_results': MCP_SEARCH_MAX_RESULTS})

    if 'project' in request.args:
        effective_project = normalize_project(request.args.get('project', ''))
    else:
        effective_project = detect_project()
    results = hybrid_search(
        query,
        project=effective_project,
        max_results=MCP_SEARCH_MAX_RESULTS,
    )

    payload = []
    for item in results:
        payload.append({
            'id': item.get('id', ''),
            'score': round(float(item.get('score', 0) or 0), 2),
            'source': item.get('source', ''),
            'project': item.get('project', '') or '',
            'category': item.get('category', '') or '',
            'text': (item.get('text', '') or '')[:160],
        })

    return jsonify({
        'query': query,
        'effective_project': effective_project,
        'max_results': MCP_SEARCH_MAX_RESULTS,
        'results': payload,
    })


def update_memory_metadata(
    memory_id: str,
    *,
    project: str | None = None,
    category: str | None = None,
) -> dict:
    """更新 Chroma metadata 中的 project/category，不重算向量。"""
    import chromadb

    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    col = client.get_collection('mem0')
    result = col.get(ids=[memory_id], include=['metadatas'])
    ids = result.get('ids') or []
    if not ids:
        raise ValueError(f'记忆不存在: {memory_id}')

    old_meta = dict((result.get('metadatas') or [{}])[0] or {})
    new_meta = dict(old_meta)
    changes: list[tuple[str, str, str]] = []

    if project is not None:
        old_project = normalize_project(str(old_meta.get('project', '') or ''))
        new_project = normalize_project(project)
        if old_project != new_project:
            new_meta['project'] = new_project
            changes.append(('project', old_project, new_project))

    if category is not None:
        old_category = normalize_category(str(old_meta.get('category', '') or ''))
        new_meta['category'] = category
        apply_category_metadata(new_meta)
        new_category = normalize_category(new_meta.get('category', ''))
        if old_category != new_category:
            changes.append(('category', old_category, new_category))

    if not changes:
        normalized_project = normalize_project(str(new_meta.get('project', '') or ''))
        normalized_category = normalize_category(str(new_meta.get('category', '') or ''))
        return {
            'changed': False,
            'project': normalized_project,
            'category': normalized_category,
            'category_label': CATEGORY_LABELS.get(normalized_category, normalized_category),
            'category_raw': str(new_meta.get('category_raw', '') or ''),
        }

    col.update(ids=[memory_id], metadatas=[new_meta])

    from memory_sync import sync_active_update_meta

    sync_active_update_meta(
        memory_id,
        project=normalize_project(str(new_meta.get('project', '') or '')) if project is not None else None,
        category=normalize_category(str(new_meta.get('category', '') or '')) if category is not None else None,
    )

    for field, old_val, new_val in changes:
        if field == 'category':
            record_event(
                'CATEGORY_CHANGE',
                memory_id,
                category=new_val,
                note=f'{old_val} → {new_val}',
                actor='mem_viewer',
            )
        elif field == 'project':
            record_event(
                'GROOMING',
                memory_id,
                note=f'project: {old_val or "全局"} → {new_val or "全局"}',
                actor='mem_viewer',
            )

    normalized_project = normalize_project(str(new_meta.get('project', '') or ''))
    normalized_category = normalize_category(str(new_meta.get('category', '') or ''))
    return {
        'changed': True,
        'project': normalized_project,
        'category': normalized_category,
        'category_label': CATEGORY_LABELS.get(normalized_category, normalized_category),
        'category_raw': str(new_meta.get('category_raw', '') or ''),
    }


@app.route('/api/update/<memory_id>', methods=['POST'])
def update_memory(memory_id: str):
    """更新记忆的 project/category（仅 metadata，不重嵌向量）。"""
    body = request.get_json(silent=True) or {}
    has_project = 'project' in body
    has_category = 'category' in body
    if not has_project and not has_category:
        return jsonify({'ok': False, 'error': '至少提供 project 或 category'}), 400

    project = body.get('project') if has_project else None
    category = body.get('category') if has_category else None
    if has_category:
        normalized = normalize_category(category)
        if normalized not in VALID_CATEGORIES:
            return jsonify({'ok': False, 'error': f'无效 category: {category}'}), 400

    try:
        result = update_memory_metadata(memory_id, project=project, category=category)
        return jsonify({'ok': True, **result})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/delete/<memory_id>', methods=['POST'])
def delete(memory_id):
    """删除记忆：memory_sync 多表事务 + Chroma。"""
    from memory_delete import archive_delete
    from memory_sync import SyncError

    payload = request.get_json(silent=True) or {}
    reason = str(payload.get('reason', '') or '').strip()
    if not reason:
        return jsonify({'ok': False, 'error': '必须填写删除原因（reason）'}), 400

    try:
        result = archive_delete(
            memory_id,
            reason,
            actor='mem_viewer',
            source='mem_viewer',
        )
        return jsonify({'ok': True, 'counts': result.get('counts', {})})
    except SyncError as exc:
        return jsonify({'ok': False, 'error': str(exc), 'sync_pending': True}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)})


if __name__ == '__main__':
    print(f'mem0 记忆图谱启动: http://{HOST}:{PORT}')
    app.run(host=HOST, port=PORT, debug=False)