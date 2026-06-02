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
    extract_keywords,
    hybrid_search,
    normalize_project,
)

# 配置
HOST = 'localhost'
PORT = 8765
DEFAULT_USER = os.getenv('MEM0_USER_ID', 'default-user')

# 项目颜色映射（固定 8 色，超出后循环）
_PROJECT_COLORS = [
    '#e74c3c', '#3498db', '#2ecc71', '#f39c12',
    '#9b59b6', '#1abc9c', '#e67e22', '#34495e',
]
_GLOBAL_COLOR = '#95a5a6'


def load_all_memories() -> list[dict]:
    """从 history.db 和 ChromaDB 合并加载全部记忆，包含变更厚度。"""
    conn = sqlite3.connect(HISTORY_DB)
    try:
        deleted_rows = conn.execute(
            "SELECT DISTINCT memory_id FROM history WHERE event = 'DELETE' AND memory_id IS NOT NULL"
        ).fetchall()
        deleted_ids = {row[0] for row in deleted_rows if row[0]}

        rows = conn.execute(
            "SELECT memory_id, new_memory, old_memory, created_at FROM history WHERE is_deleted = 0 ORDER BY created_at DESC"
        ).fetchall()

        # 变更厚度：统计每个 memory_id 的 UPDATE 事件数
        update_rows = conn.execute(
            "SELECT memory_id, count(*) FROM history WHERE event = 'UPDATE' AND is_deleted = 0 GROUP BY memory_id"
        ).fetchall()
        update_count_map = {row[0]: row[1] for row in update_rows if row[0]}
    finally:
        conn.close()

    text_map: dict[str, str] = {}
    created_at_map: dict[str, str] = {}
    for memory_id, new_memory, old_memory, created_at in rows:
        if not memory_id or memory_id in deleted_ids:
            continue
        text = (new_memory or old_memory or '').strip()
        if text and memory_id not in text_map:
            text_map[memory_id] = text
            created_at_map[memory_id] = created_at or ''

    metadata_map = _load_chroma_metadata()

    memories = []
    for memory_id, text in text_map.items():
        meta = metadata_map.get(memory_id, {})
        # 归一化 project="全局" → ""，统一灰色显示
        raw_project = meta.get('project', '')
        project = '' if raw_project == '全局' else raw_project
        memories.append({
            'id': memory_id,
            'text': text,
            'project': project,
            'category': meta.get('category', ''),
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

  .main { display:flex; flex:1; overflow:hidden; }
  #graph-container { flex:1; }
  #detail-panel { width:320px; padding:16px; background:#16213e; border-left:1px solid #0f3460; overflow-y:auto; display:none; }
  #detail-panel.visible { display:block; }

  .detail-title { font-size:16px; font-weight:600; margin-bottom:12px; color:#3498db; }
  .detail-section { margin-bottom:16px; }
  .detail-label { font-size:12px; color:#7f8c8d; margin-bottom:4px; }
  .detail-text { font-size:14px; line-height:1.6; white-space:pre-wrap; }
  .detail-meta { font-size:12px; color:#95a5a6; }
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
  <button class="btn-search" onclick="doSearch()">搜索</button>
  <button class="btn-reset" onclick="resetGraph()">重置</button>
</div>

<div class="legend-panel">
  <h4>图例</h4>
  {% for project, color in project_color_items %}
  <div class="legend-item">
    <div class="legend-dot" style="background:{{ color }}"></div>
    <span>{{ project }}</span>
  </div>
  {% endfor %}
  <div class="legend-item">
    <div class="legend-dot" style="background:{{ global_color }}"></div>
    <span>全局</span>
  </div>
  <hr style="border-color:#0f3460;margin:8px 0" />
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
      <div class="detail-meta" id="detail-scope"></div>
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
    document.getElementById('detail-scope').textContent = '项目: ' + (mem.project || '全局') + ' | 分类: ' + (mem.category || '未知') + ' | 创建: ' + (mem.created_at || '未知');
    document.getElementById('detail-meta').textContent = JSON.stringify(mem.metadata, null, 2);
    const changeEmoji = thick.change >= 2 ? '★' : thick.change === 1 ? '◆' : '●';
    document.getElementById('detail-thickness').textContent =
      changeEmoji + ' 变更:' + thick.change + '  |  连接:' + thick.connection + '  |  重复:' + thick.repetition;
    document.getElementById('detail-id').textContent = id;
    document.getElementById('detail-panel').classList.add('visible');
  }

  function hideDetail() {
    selectedId = null;
    document.getElementById('detail-panel').classList.remove('visible');
  }

  function doSearch() {
    const query = document.getElementById('search-input').value.trim();
    if (!query) { return; }
    const currentProject = document.getElementById('project-filter').value;
    fetch('/search?q=' + encodeURIComponent(query))
      .then(r => r.json())
      .then(matchIds => {
        const matchSet = new Set(matchIds);
        nodes.update(nodesData.filter(n => !deletedNodeIds.has(n.id)).map(n => {
          const mem = memoriesMap[n.id];
          const isHiddenByProject = currentProject && mem && mem.project !== currentProject;
          return {
            ...n,
            opacity: isHiddenByProject ? 0 : (matchSet.has(n.id) ? 1.0 : 0.15),
            hidden: isHiddenByProject ? true : false,
          };
        }));

        const edgeIds = edges.getIds();
        const edgeUpdates = edgeIds.map(eid => {
          const edge = edges.get(eid);
          const bothMatch = matchSet.has(edge.from) && matchSet.has(edge.to);
          return { id: eid, color: bothMatch ? { color: '#3498db', highlight: '#3498db' } : { color: '#1a1a2e', highlight: '#1a1a2e' }, opacity: bothMatch ? 1.0 : 0.08 };
        });
        edges.update(edgeUpdates);

        const totalCount = nodesData.filter(n => !deletedNodeIds.has(n.id)).length;
        document.getElementById('stats').textContent =
          '匹配 ' + matchIds.length + ' / ' + totalCount + ' 条记忆';
      })
      .catch(err => {
        console.error('Search failed:', err);
        document.getElementById('stats').textContent = '搜索失败，请重试';
      });
  }

  document.getElementById('project-filter').addEventListener('change', function() {
    const query = document.getElementById('search-input').value.trim();
    if (query) { doSearch(); return; }
    const project = this.value;
    nodes.update(nodesData.filter(n => !deletedNodeIds.has(n.id)).map(n => {
      const mem = memoriesMap[n.id];
      const isHidden = project && mem && mem.project !== project;
      return {
        ...n,
        hidden: isHidden ? true : false,
        opacity: isHidden ? 0 : 1.0,
      };
    }));
  });

  document.getElementById('search-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doSearch();
  });

  function resetGraph() {
    document.getElementById('search-input').value = '';
    document.getElementById('project-filter').value = '';
    nodes.update(nodesData.filter(n => !deletedNodeIds.has(n.id)).map(n => ({
      ...n,
      opacity: 1.0,
      hidden: false,
    })));
    edges.update(edgesData.map(e => ({...e})));
    hideDetail();
    updateStats();
  }

  function deleteMemory() {
    if (!selectedId) return;
    if (!confirm('确认删除记忆 ' + selectedId + '？')) return;
    fetch('/delete/' + selectedId, { method: 'POST' })
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
    project_colors = assign_project_colors(memories)
    edges = compute_edges(memories)
    thickness = compute_thickness(memories, edges)

    nodes_json = json.dumps([
        {
            'id': m['id'],
            'label': m['text'][:30] + ('...' if len(m['text']) > 30 else ''),
            'title': m['text'][:60] + ('...' if len(m['text']) > 60 else ''),
            'color': project_colors.get(m['project'], _GLOBAL_COLOR),
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

    return render_template_string(
        HTML_TEMPLATE,
        nodes_json=nodes_json,
        edges_json=edges_json,
        memories_map_json=memories_map_json,
        thickness_json=thickness_json,
        projects=projects,
        vis_js_cdn=VIS_JS_CDN,
        project_color_items=[(p, project_colors[p]) for p in sorted(project_colors)],
        global_color=_GLOBAL_COLOR,
    )


@app.route('/search')
def search():
    """搜索接口：返回匹配的 memory ID 列表。"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    project = request.args.get('project', '')
    results = hybrid_search(query, project=normalize_project(project), max_results=20)
    match_ids = [r['id'] for r in results]
    return jsonify(match_ids)


@app.route('/delete/<memory_id>', methods=['POST'])
def delete(memory_id):
    """删除记忆。直接操作 ChromaDB + history.db，无需 LLM。"""
    try:
        # 1. 从 ChromaDB 删除向量
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        col.delete(ids=[memory_id])

        # 2. 在 history.db 标记 DELETE 事件
        conn = sqlite3.connect(HISTORY_DB)
        try:
            conn.execute(
                "INSERT INTO history (id, memory_id, event, is_deleted, created_at) VALUES (?, ?, 'DELETE', 1, ?)",
                (memory_id + '_del', memory_id, time.strftime('%Y-%m-%dT%H:%M:%S')),
            )
            conn.commit()
        finally:
            conn.close()

        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)})


if __name__ == '__main__':
    print(f'mem0 记忆图谱启动: http://{HOST}:{PORT}')
    app.run(host=HOST, port=PORT, debug=False)