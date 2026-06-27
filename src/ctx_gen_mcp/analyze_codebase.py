"""
analyze_codebase.py -- Codebase analysis tool for clustering and coding standards.

This module provides functions to:
1. Cluster modules by functional similarity (multi-dimensional)
2. Identify module boundaries and responsibilities
3. Extract coding standards (including I-type interface priority)

Used by the `analyze_codebase` MCP tool in server.py.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 1. Multi-dimensional similarity analysis
# ---------------------------------------------------------------------------

def _extract_function_names(public_api: list) -> list[str]:
    """Extract function names from public_api list."""
    names = []
    for item in public_api:
        if isinstance(item, str):
            # Parse "function_name(params) -> return_type" format
            m = re.match(r'^(\w+)', item)
            if m:
                names.append(m.group(1))
        elif isinstance(item, dict):
            name = item.get('name', '')
            if name:
                names.append(name)
    return names


def _extract_struct_names(key_data_structures: list) -> list[str]:
    """Extract struct/class names from key_data_structures list."""
    names = []
    for item in key_data_structures:
        if isinstance(item, str):
            # Parse "struct_name: description" or "class_name: description"
            m = re.match(r'^(\w+)', item)
            if m:
                names.append(m.group(1))
        elif isinstance(item, dict):
            name = item.get('name', '')
            if name:
                names.append(name)
    return names


def _compute_dependency_similarity(mod1_deps: list, mod2_deps: list) -> float:
    """Compute similarity based on shared dependencies.

    Returns: Jaccard similarity of dependency sets.
    """
    set1 = set(mod1_deps)
    set2 = set(mod2_deps)
    if not set1 and not set2:
        return 0.0
    overlap = len(set1 & set2)
    union = len(set1 | set2)
    return overlap / union if union > 0 else 0.0


def _compute_struct_similarity(mod1_structs: list, mod2_structs: list) -> float:
    """Compute similarity based on shared data structures.

    Returns: Jaccard similarity of struct name sets.
    """
    set1 = set(mod1_structs)
    set2 = set(mod2_structs)
    if not set1 and not set2:
        return 0.0
    overlap = len(set1 & set2)
    union = len(set1 | set2)
    return overlap / union if union > 0 else 0.0


def _compute_naming_similarity(mod1_functions: list, mod2_functions: list) -> float:
    """Compute similarity based on function naming patterns.

    Looks for:
    - Common prefixes (e.g., "ssl_read", "ssl_write" -> prefix "ssl")
    - Common suffixes (e.g., "_init", "_cleanup")
    - Common substrings

    Returns: similarity score 0.0-1.0
    """
    if not mod1_functions and not mod2_functions:
        return 0.0

    # Extract prefixes (first word before '_' or camelCase split)
    def extract_prefixes(funcs):
        prefixes = []
        for f in funcs:
            # snake_case prefix
            if '_' in f:
                prefixes.append(f.split('_')[0])
            # CamelCase prefix (first word)
            else:
                m = re.match(r'^([A-Z][a-z]+)', f)
                if m:
                    prefixes.append(m.group(1).lower())
        return prefixes

    pref1 = set(extract_prefixes(mod1_functions))
    pref2 = set(extract_prefixes(mod2_functions))

    if not pref1 and not pref2:
        return 0.0

    overlap = len(pref1 & pref2)
    union = len(pref1 | pref2)
    return overlap / union if union > 0 else 0.0


def _compute_module_similarity(ctx1: dict, ctx2: dict) -> dict:
    """Compute multi-dimensional similarity between two modules.

    Returns dict with similarity scores and reasons.
    """
    # Extract data
    deps1 = ctx1.get('dependencies', [])
    deps2 = ctx2.get('dependencies', [])
    structs1 = _extract_struct_names(ctx1.get('key_data_structures', []))
    structs2 = _extract_struct_names(ctx2.get('key_data_structures', []))
    funcs1 = _extract_function_names(ctx1.get('public_api', []))
    funcs2 = _extract_function_names(ctx2.get('public_api', []))

    # Compute similarities
    dep_sim = _compute_dependency_similarity(deps1, deps2)
    struct_sim = _compute_struct_similarity(structs1, structs2)
    name_sim = _compute_naming_similarity(funcs1, funcs2)

    # Weighted average (configurable weights)
    weights = {'dependency': 0.3, 'struct': 0.4, 'naming': 0.3}
    overall = (dep_sim * weights['dependency'] +
               struct_sim * weights['struct'] +
               name_sim * weights['naming'])

    # Gather reasons
    reasons = []
    if dep_sim > 0.3:
        shared_deps = set(deps1) & set(deps2)
        reasons.append(f"Shared dependencies: {', '.join(shared_deps)}")
    if struct_sim > 0.3:
        shared_structs = set(structs1) & set(structs2)
        reasons.append(f"Shared data structures: {', '.join(shared_structs)}")
    if name_sim > 0.3:
        pref1 = set(re.split(r'[_\s]', f)[0] for f in funcs1 if '_' in f or f[0].isupper())
        pref2 = set(re.split(r'[_\s]', f)[0] for f in funcs2 if '_' in f or f[0].isupper())
        shared_prefixes = pref1 & pref2
        reasons.append(f"Common naming prefixes: {', '.join(shared_prefixes)}")

    return {
        'overall': round(overall, 3),
        'dependency': round(dep_sim, 3),
        'struct': round(struct_sim, 3),
        'naming': round(name_sim, 3),
        'reasons': reasons,
    }


# ---------------------------------------------------------------------------
# 2. Cluster modules by similarity
# ---------------------------------------------------------------------------

def cluster_modules(ctx_dir: Path, skeleton: dict, similarity_threshold: float = 0.4) -> list[dict]:
    """Cluster modules by functional similarity.

    Uses a simple greedy clustering algorithm:
    1. Start with each module as its own cluster
    2. Merge clusters if average pairwise similarity > threshold

    Returns list of clusters, each with:
      - cluster_id: str
      - modules: list of module IDs
      - domain: str (inferred from skeleton)
      - similarity_score: float
      - reasons: list of str
    """
    # Load all ctx JSONs
    ctx_data = {}
    for mid in skeleton.get('modules', []):
        if isinstance(mid, dict):
            mid = mid.get('id', '')
        ctx_path = ctx_dir / f"{mid}.json"
        if ctx_path.exists():
            try:
                data = json.loads(ctx_path.read_text(encoding='utf-8'))
                ctx_data[mid] = data
            except Exception:
                continue

    if len(ctx_data) < 2:
        return [{'cluster_id': 'cluster_1',
                 'modules': list(ctx_data.keys()),
                 'domain': 'unknown',
                 'similarity_score': 1.0,
                 'reasons': ['Only one module']}]

    # Compute pairwise similarities
    modules = list(ctx_data.keys())
    sim_matrix = {}
    for i, m1 in enumerate(modules):
        for j, m2 in enumerate(modules):
            if i >= j:
                continue
            sim = _compute_module_similarity(ctx_data[m1], ctx_data[m2])
            sim_matrix[(m1, m2)] = sim
            sim_matrix[(m2, m1)] = sim

    # Greedy clustering
    clusters = [[m] for m in modules]
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                # Compute average pairwise similarity between clusters
                sims = []
                for m1 in clusters[i]:
                    for m2 in clusters[j]:
                        key = (m1, m2) if (m1, m2) in sim_matrix else (m2, m1)
                        if key in sim_matrix:
                            sims.append(sim_matrix[key]['overall'])
                if sims and sum(sims) / len(sims) >= similarity_threshold:
                    clusters[i] = clusters[i] + clusters[j]
                    clusters.pop(j)
                    merged = True
                    break
            if merged:
                break

    # Build result
    result = []
    for idx, cluster in enumerate(clusters):
        # Find domain from skeleton
        domain = 'unknown'
        for mod in skeleton.get('modules', []):
            if isinstance(mod, dict) and mod.get('id') in cluster:
                domain = mod.get('domain', 'unknown')
                break

        # Compute average similarity within cluster
        sims = []
        reasons = []
        for i, m1 in enumerate(cluster):
            for m2 in cluster[i+1:]:
                key = (m1, m2) if (m1, m2) in sim_matrix else (m2, m1)
                if key in sim_matrix:
                    sims.append(sim_matrix[key]['overall'])
                    reasons.extend(sim_matrix[key]['reasons'])

        avg_sim = sum(sims) / len(sims) if sims else 1.0
        result.append({
            'cluster_id': f'cluster_{idx + 1}',
            'modules': cluster,
            'domain': domain,
            'similarity_score': round(avg_sim, 3),
            'reasons': list(set(reasons)),  # deduplicate
        })

    return result


# ---------------------------------------------------------------------------
# 3. Identify I-type interfaces
# ---------------------------------------------------------------------------

def identify_i_interfaces(ctx_dir: Path, skeleton: dict) -> list[dict]:
    """Identify I-type interfaces (function/struct names starting with 'I').

    Returns list of interfaces with:
      - interface_name: str
      - module: str
      - type: 'function' | 'struct' | 'class'
      - called_by: list of modules (from dependencies analysis)
      - priority: int (1 = highest priority)
    """
    interfaces = []
    ctx_data = {}

    # Load all ctx JSONs
    for mid in skeleton.get('modules', []):
        if isinstance(mid, dict):
            mid = mid.get('id', '')
        ctx_path = ctx_dir / f"{mid}.json"
        if ctx_path.exists():
            try:
                data = json.loads(ctx_path.read_text(encoding='utf-8'))
                ctx_data[mid] = data
            except Exception:
                continue

    # Scan for I-type interfaces
    for mid, data in ctx_data.items():
        # Check public_api for I-type functions
        for api in data.get('public_api', []):
            func_name = ''
            if isinstance(api, str):
                m = re.match(r'^(\w+)', api)
                if m:
                    func_name = m.group(1)
            elif isinstance(api, dict):
                func_name = api.get('name', '')

            if func_name and func_name.startswith('I'):
                # Find which modules call this
                called_by = []
                for other_mid, other_data in ctx_data.items():
                    if other_mid == mid:
                        continue
                    if mid in other_data.get('dependencies', []):
                        called_by.append(other_mid)

                interfaces.append({
                    'interface_name': func_name,
                    'module': mid,
                    'type': 'function',
                    'called_by': called_by,
                    'priority': 1,  # I-type = highest priority
                })

        # Check key_data_structures for I-type structs/classes
        for struct in data.get('key_data_structures', []):
            struct_name = ''
            if isinstance(struct, str):
                m = re.match(r'^(\w+)', struct)
                if m:
                    struct_name = m.group(1)
            elif isinstance(struct, dict):
                struct_name = struct.get('name', '')

            if struct_name and struct_name.startswith('I'):
                interfaces.append({
                    'interface_name': struct_name,
                    'module': mid,
                    'type': 'struct',
                    'called_by': [],
                    'priority': 1,
                })

    # Sort by interface name
    interfaces.sort(key=lambda x: x['interface_name'])
    return interfaces


# ---------------------------------------------------------------------------
# 4. Generate output documents
# ---------------------------------------------------------------------------

def generate_module_boundaries(ctx_dir: Path, skeleton: dict, clusters: list[dict], output_path: Path):
    """Generate docs/MODULE_BOUNDARIES.md.

    This document defines:
    - Each module's responsibility
    - Module clusters (functionally similar modules)
    - Where new code should be placed
    """
    ctx_data = {}
    for mid in skeleton.get('modules', []):
        if isinstance(mid, dict):
            mid = mid.get('id', '')
        ctx_path = ctx_dir / f"{mid}.json"
        if ctx_path.exists():
            try:
                data = json.loads(ctx_path.read_text(encoding='utf-8'))
                ctx_data[mid] = data
            except Exception:
                continue

    lines = ['# Module Boundaries and Responsibilities\n']
    lines.append('This document defines the responsibility boundaries of each module.')
    lines.append('Use this when deciding where to place new code.\n')

    # Section 1: Module clusters
    lines.append('## Module Clusters\n')
    lines.append('Functionally similar modules should be kept together.')
    lines.append('The following clusters were identified by multi-dimensional analysis:\n')
    for cluster in clusters:
        lines.append(f'### {cluster["cluster_id"]} (domain: {cluster["domain"]})')
        lines.append(f'**Similarity score**: {cluster["similarity_score"]}')
        lines.append(f'**Modules**: {", ".join(cluster["modules"])}')
        if cluster['reasons']:
            lines.append('**Why they cluster together**:')
            for reason in cluster['reasons']:
                lines.append(f'- {reason}')
        lines.append('')

    # Section 2: Per-module responsibilities
    lines.append('## Module Responsibilities\n')
    for mid, data in ctx_data.items():
        lines.append(f'### {mid}')
        purpose = data.get('purpose', 'Unknown')
        lines.append(f'**Purpose**: {purpose}')
        lines.append(f'**Domain**: {data.get("domain", "unknown")}')
        lines.append(f'**Tags**: {", ".join(data.get("tags", []))}')
        lines.append(f'**Dependencies**: {", ".join(data.get("dependencies", [])) or "none"}')
        lines.append('')

        # Suggested placement for new code
        lines.append('**New code placement guide**:')
        lines.append(f'- If implementing features related to: {purpose}')
        lines.append(f'  → Place in **{mid}** or a sub-module')
        if data.get('dependencies'):
            lines.append(f'- If the new code depends on: {", ".join(data.get("dependencies", [])[:3])}')
            lines.append(f'  → Consider placing in **{mid}** (to avoid circular deps)')
        lines.append('')

    output_path.write_text('\n'.join(lines), encoding='utf-8')


def generate_coding_standards(ctx_dir: Path, skeleton: dict, clusters: list[dict],
                              i_interfaces: list[dict], output_path: Path):
    """Generate docs/CODING_STANDARDS.md.

    This document captures coding conventions and interface priority rules.
    """
    ctx_data = {}
    for mid in skeleton.get('modules', []):
        if isinstance(mid, dict):
            mid = mid.get('id', '')
        ctx_path = ctx_dir / f"{mid}.json"
        if ctx_path.exists():
            try:
                data = json.loads(ctx_path.read_text(encoding='utf-8'))
                ctx_data[mid] = data
            except Exception:
                continue

    lines = ['# Coding Standards and Conventions\n']
    lines.append('This document captures the coding standards and conventions')
    lines.append('observed in this codebase. Follow these when writing new code.\n')

    # Section 1: Naming conventions (per domain)
    lines.append('## Naming Conventions\n')
    domain_functions = defaultdict(list)
    for mid, data in ctx_data.items():
        domain = data.get('domain', 'unknown')
        funcs = _extract_function_names(data.get('public_api', []))
        domain_functions[domain].extend(funcs)

    for domain, funcs in domain_functions.items():
        if not funcs:
            continue
        lines.append(f'### Domain: {domain}')
        # Analyze naming pattern
        has_snake = any('_' in f for f in funcs)
        has_camel = any(re.match(r'^[a-z]+[A-Z]', f) for f in funcs)
        prefixes = defaultdict(int)
        for f in funcs:
            if '_' in f:
                prefixes[f.split('_')[0]] += 1
        common_prefixes = sorted(prefixes.items(), key=lambda x: -x[1])[:5]

        if has_snake:
            lines.append('- **Style**: snake_case')
        if has_camel:
            lines.append('- **Style**: camelCase')
        if common_prefixes:
            lines.append(f'- **Common prefixes**: {", ".join(p + f" ({c})" for p, c in common_prefixes)}')
        lines.append('')
        lines.append('**Examples**:')
        for f in funcs[:5]:
            lines.append(f'- `{f}`')
        lines.append('')

    # Section 2: Interface priority rules
    lines.append('## Interface Priority Rules\n')
    lines.append('When calling interfaces, follow this priority order:\n')

    if i_interfaces:
        lines.append('### I-type Interfaces (Highest Priority)')
        lines.append('Interfaces starting with `I` are the preferred API.')
        lines.append('Always use these before falling back to internal APIs.\n')
        lines.append('| Interface | Module | Type | Called By |')
        lines.append('|----------|--------|------|-----------|')
        for iface in i_interfaces:
            called_by_str = ', '.join(iface['called_by']) if iface['called_by'] else '(none)'
            lines.append(f"| `{iface['interface_name']}` | {iface['module']} | {iface['type']} | {called_by_str} |")
        lines.append('')
        lines.append('**Rule**: When implementing new features, prefer calling I-type interfaces.')
        lines.append('If an I-type interface exists for the functionality you need, use it.')
        lines.append('')

    # Section 3: Error handling patterns
    lines.append('## Error Handling Patterns\n')
    error_patterns = Counter()
    for mid, data in ctx_data.items():
        design = data.get('design_notes', '')
        if isinstance(design, str):
            if 'errno' in design.lower() or 'return error' in design.lower():
                error_patterns['return error code'] += 1
            if 'goto' in design.lower():
                error_patterns['goto cleanup'] += 1
            if 'try' in design.lower() or 'catch' in design.lower():
                error_patterns['try/catch'] += 1
            if 'panic' in design.lower():
                error_patterns['panic'] += 1

    if error_patterns:
        lines.append('Observed error handling patterns:')
        for pattern, count in error_patterns.most_common():
            lines.append(f'- **{pattern}** (used in {count} modules)')
        lines.append('')
        lines.append('**Recommendation**: Follow the dominant pattern in the domain.')
        lines.append('')

    # Section 4: Module clustering recommendations
    lines.append('## Module Clustering Recommendations\n')
    lines.append('Based on functional similarity analysis:\n')
    for cluster in clusters:
        if cluster['similarity_score'] < 0.3:
            continue
        lines.append(f'- **{cluster["cluster_id"]}**: {", ".join(cluster["modules"])}')
        lines.append(f'  → These modules are functionally similar (score: {cluster["similarity_score"]}).')
        lines.append(f'  → New code related to these modules should be placed nearby.')
        lines.append('')

    output_path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------------------
# 5. Main analysis function (called by MCP tool)
# ---------------------------------------------------------------------------

def analyze_codebase(project_dir: str, ctx_dir: str = '.ctx-cache/ctx',
                    skeleton_path: str = '.ctx-cache/skeleton.json',
                    out_docs: str = 'docs') -> dict:
    """Main analysis function.

    Returns dict with analysis results.
    """
    root = Path(project_dir).resolve()
    ctx = Path(ctx_dir).resolve() if Path(ctx_dir).is_absolute() else root / ctx_dir
    skel_path = Path(skeleton_path).resolve() if Path(skeleton_path).is_absolute() else root / skeleton_path
    out = Path(out_docs).resolve() if Path(out_docs).is_absolute() else root / out_docs

    # Pre-condition checks
    if not root.exists():
        return {'status': 'error', '_fatal_errors': [f'project_dir does not exist: {root}']}
    if not ctx.exists():
        return {'status': 'error', '_fatal_errors': [f'ctx_dir does not exist: {ctx}']}
    if not skel_path.exists():
        return {'status': 'error', '_fatal_errors': [f'skeleton_path does not exist: {skel_path}']}

    # Load skeleton
    try:
        skeleton = json.loads(skel_path.read_text(encoding='utf-8'))
    except Exception as e:
        return {'status': 'error', '_fatal_errors': [f'Cannot read skeleton JSON: {e}']}

    # Create output dir
    out.mkdir(parents=True, exist_ok=True)

    # 1. Cluster modules
    clusters = cluster_modules(ctx, skeleton, similarity_threshold=0.4)

    # 2. Identify I-type interfaces
    i_interfaces = identify_i_interfaces(ctx, skeleton)

    # 3. Generate output documents
    boundaries_path = out / 'MODULE_BOUNDARIES.md'
    standards_path = out / 'CODING_STANDARDS.md'
    generate_module_boundaries(ctx, skeleton, clusters, boundaries_path)
    generate_coding_standards(ctx, skeleton, clusters, i_interfaces, standards_path)

    # 4. Build recommendations
    recommendations = []
    for cluster in clusters:
        if len(cluster['modules']) > 1 and cluster['similarity_score'] > 0.6:
            recommendations.append(
                f"Modules {', '.join(cluster['modules'])} are highly similar "
                f"(score {cluster['similarity_score']}). Consider merging or keeping them together."
            )

    return {
        'status': 'success',
        'modules_analyzed': len(skeleton.get('modules', [])),
        'clusters': clusters,
        'i_interfaces': i_interfaces,
        'output_files': {
            'module_boundaries': str(boundaries_path),
            'coding_standards': str(standards_path),
        },
        'recommendations': recommendations,
    }
