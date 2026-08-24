# Zotero Obsidian Literature Skill

一个面向 Codex 的本地文献管理 Skill：从 Zotero 只读获取文献元数据、分类与已索引全文，生成有证据依据的中文结构化摘要，并在 Obsidian 中维护文献笔记、动态总表、一级分类视图和同步状态。

> 当前版本不是后台常驻同步服务。每次新增、修改或移动文献后，需要在 Codex 中发出一次同步指令。

## 功能概览

- 读取 Zotero 中新增或发生变化的文献，不修改 Zotero 数据库和条目。
- 优先基于 PDF 全文总结；全文不可用时明确标记为摘要或元数据级总结。
- 为每篇文献生成结构化 Obsidian Markdown 笔记。
- 自动维护 Obsidian `Library.base` 文献总表。
- 将 Zotero 完整分类路径写入 `zotero_collections`。
- 每个 Zotero 一级目录生成一个 Obsidian Base，子目录作为标签和 Dashboard 嵌套导航展示。
- 支持既有历史目录的分批回填、持续同步和进度恢复。
- 支持按需生成单篇论文的详细解读。
- 对移出监控目录或从 Zotero 删除的文献先预览，再以可恢复方式归档 Obsidian 笔记。
- 保留文献笔记中由用户手写的“我的笔记”区域。
- 支持用 `no-ai` 或 `codex-ignore` 标签排除不希望处理的条目。

## 工作原理

```text
Zotero 本地 API（只读）
        │
        ▼
zotero_sync.py 扫描元数据、分类、附件和已索引全文
        │
        ▼
生成待处理 JSON packet
        │
        ▼
Codex 按固定 schema 阅读论文并生成中文 summary JSON
        │
        ▼
zotero_sync.py 校验并渲染 Markdown
        │
        ├── Zotero/Papers/*.md
        ├── Zotero/Library.base
        ├── Zotero/Collections/<一级目录>.base
        └── Zotero/Zotero Dashboard.md
```

Python 脚本负责可重复、确定性的发现、状态跟踪、校验和文件渲染；科学问题、数据集、方法、结论和局限性的解释由 Codex 根据可用证据完成。脚本本身不调用任何大模型 API。

## 生成的文献字段

每篇笔记会记录：

- `title`：题名
- `publication_date` / `publication_year`：发表时间
- `authors`：作者
- `keywords`：论文作者提供的关键词；没有时保持为空，不自动编造
- `zotero_collections`：Zotero 分类路径标签
- `scientific_question`：科学问题或知识缺口
- `datasets`：数据集、样本、实验材料、研究区域和时间范围
- `methods`：研究设计、分析、模型、实验和验证方法
- `main_findings`：主要结果
- `scientific_problem_solved`：相对已有知识缺口解决的问题和贡献
- `limitations`：作者声明或由论文证据支持的适用边界
- `summary_basis`：`fulltext`、`abstract` 或 `metadata`
- `evidence_notes`：章节名或可靠的页码线索

## Zotero 分类与 Obsidian 视图

文献文件统一存放在 `Zotero/Papers`，避免在 Zotero 和 Obsidian 之间维护两套物理目录。分类关系通过 Properties 和 Obsidian Bases 动态展示。

例如 Zotero 路径：

```text
环境遥感 / 植被遥感 / 植被指数
```

会写为：

```yaml
zotero_collections:
  - "#环境遥感"
  - "#植被遥感"
  - "#植被指数"
```

系统只为一级目录生成 `环境遥感.base`。Dashboard 将下级目录显示为嵌套列表，并显示包含后代目录的文献数量。

## 环境要求

- Codex 桌面应用或支持 Agent Skills 的 Codex 环境
- Zotero 桌面端正在运行
- Zotero 本地 API 可访问，默认地址为 `http://localhost:23119/api`
- 需要全文总结时，PDF 已添加到 Zotero 且已完成全文索引
- Obsidian 支持 Bases
- Python 3.10 或更高版本
- 脚本仅依赖 Python 标准库，不需要 `pip install`

## 安装

推荐把 Skill 安装到 Obsidian 仓库内部，使仓库、模板和 Skill 可以一起迁移。

### 方法一：下载 ZIP

1. 下载本仓库 ZIP 并解压。
2. 将整个 `zotero-obsidian-literature` 文件夹复制到：

   ```text
   <你的 Obsidian 仓库>/.agents/skills/zotero-obsidian-literature
   ```

3. 确认最终路径中直接存在 `SKILL.md`：

   ```text
   <vault>/.agents/skills/zotero-obsidian-literature/SKILL.md
   ```

4. 在 Codex 中打开该 Obsidian 仓库或包含该 Skill 的工作区。

### 方法二：Git 克隆

在 Obsidian 仓库根目录执行：

```powershell
git clone <本仓库地址> .agents/skills/zotero-obsidian-literature
```

如果目标目录已经存在，请先备份自定义修改，不要直接覆盖。

## 首次初始化

在 Obsidian 仓库根目录运行：

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py init-vault --vault .
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py probe --vault .
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py bootstrap --vault .
```

各命令作用：

1. `init-vault`：创建 `Zotero` 目录、Dashboard、Library Base、配置和同步状态文件。
2. `probe`：检查 Zotero 本地 API、库类型和服务器标识。
3. `bootstrap`：将当前 Zotero 库版本登记为“新文献同步”的基线，不导入已有历史文献。

如果希望导入以前已经存入 Zotero 的论文，请在初始化后登记一个一级目录进行历史回填，而不是跳过 `bootstrap`。

## 在 Codex 中调用

新对话中直接点名 Skill，并提供 Obsidian 仓库位置和任务。例如：

```text
请使用 $zotero-obsidian-literature。
Obsidian 仓库是 D:\Files\Obsidian\MyVault。
读取并总结我刚加入 Zotero 的新文献。
```

只要 Skill 在当前工作区可发现，通常不需要反复粘贴 `SKILL.md` 的完整路径。

## 常用自然语言指令

### 同步刚加入的文献

```text
使用 $zotero-obsidian-literature，读取并总结我刚加入 Zotero 的新文献。
```

### 登记并回填一个一级目录

```text
将 Zotero 一级目录“环境遥感”设为同步目录，包含所有子目录，每批处理 5 篇，并总结第一批。
```

### 一直处理到完成

```text
使用 $zotero-obsidian-literature，同步“环境遥感”，包含所有子目录，每批处理 5 篇，直到完成。
```

### 同步全部已登记目录

```text
使用 $zotero-obsidian-literature，同步所有已登记的 Zotero 目录，每个目录最多处理 5 篇。
```

### 核对移出目录或删除的文献

先预览：

```text
核对“环境遥感”中被移动或删除的文献，先列出结果，不要归档。
```

确认结果后再归档：

```text
将刚才列出的待归档文献移入 Obsidian 的 Zotero/Archive。
```

### 详细解读单篇论文

推荐使用不会重名的 Zotero item key：

```text
使用 $zotero-obsidian-literature，详细总结 Zotero itemKey 为 XXXXXXXX 的文献，并生成单独的 Markdown 解读。
```

## 批量同步的状态与恢复

监控目录使用 Zotero 稳定的 collection key 保存，而不是仅依赖可变化的目录名称。默认每批处理5篇，以便：

- 单批全文总结可审查；
- 中断后能从同步状态继续；
- 避免一次对话加载过多论文全文；
- 及时发现没有索引全文或元数据异常的条目。

同步进度记录在：

```text
Zotero/.sync/state.json
Zotero/.sync/config.json
```

这些是仓库运行状态，不属于 Skill 发布包。不要把真实仓库中的 `.sync` 目录提交到公开仓库。

## 命令行参考

通常由 Codex 代为运行这些命令；手动排障时也可直接使用。

| 命令 | 用途 |
|---|---|
| `init-vault` | 初始化 Obsidian 文献库目录和模板 |
| `probe` | 检查 Zotero 本地 API 和服务器标识 |
| `bootstrap` | 设置只同步后续新增文献的版本基线 |
| `scan` | 扫描基线以后新增或变化的文献 |
| `collections` | 列出 Zotero 一级目录及其 collection key |
| `watch` | 登记一个需要持续同步的一级目录 |
| `unwatch` | 取消监控目录，不删除已生成笔记 |
| `backfill` | 分批加入监控目录中的历史文献 |
| `sync-watched` | 同步已登记目录中的新增、修改、移动或未处理文献 |
| `reconcile` | 预览或应用移出监控范围的文献归档 |
| `fetch` | 按 item key 刷新单篇文献的元数据和全文 packet |
| `pending` | 列出等待 Codex 总结的 packet |
| `render` | 校验 summary JSON 并渲染文献笔记与视图 |
| `render-review` | 写入单篇详细解读文档 |
| `status` | 查看同步版本、条目和监控状态 |
| `refresh-collections` | 重建一级分类 Base 和 Dashboard 导航 |

查看完整参数：

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py --help
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py <命令> --help
```

## Obsidian 输出目录

初始化后，仓库中会出现：

```text
Zotero/
├── Zotero Dashboard.md
├── Library.base
├── Papers/                 # 活跃文献笔记
├── Reviews/                # 按需生成的详细解读
├── Collections/            # 一级 Zotero 分类 Base
├── Archive/                # 可恢复归档的文献笔记
└── .sync/                  # 本地配置、状态、packet 和已处理摘要
```

每篇文献笔记包含自动生成区域和用户区域：

```markdown
<!-- codex:auto:start -->
自动生成内容
<!-- codex:auto:end -->

## 我的笔记

这里的人工批注会在后续重新同步时保留。
```

## 安全与隐私

- Zotero 始终按只读方式访问；脚本没有修改或删除 Zotero 条目的命令。
- 文献以 `zotero_item_key` 唯一识别，避免同名论文误匹配。
- 移出监控目录的笔记不会自动永久删除；`reconcile` 默认只预览。
- 应用归档后，笔记移动至 `Zotero/Archive`，以后重新进入监控目录仍可恢复。
- `no-ai` 和 `codex-ignore` 标签可以排除条目。
- Skill 发布包不应包含真实的 `Zotero/.sync`、论文 PDF、全文 packet、摘要结果或个人笔记。
- Codex 是否会使用云端模型及其数据处理方式取决于你的 Codex 产品和账户配置；处理敏感或受限制文献前请遵循所在机构的政策。

## 摘要质量规则

- 证据优先级：全文 > 摘要 > 元数据。
- 不从题名猜测数据集、方法或结论。
- 缺失信息使用“文中未明确说明”，不补写看似合理的内容。
- 作者关键词缺失时使用空数组，不生成主题词冒充作者关键词。
- 保留结果方向、适用条件和重要定量效应。
- 区分论文实际观察结果、作者解释、Codex 综合判断和模型情景预测。
- 如果全文未索引、无法读取或被截断，在笔记与交付说明中明确标注。

详细规范见：

- [`references/summary-schema.md`](references/summary-schema.md)
- [`references/summary-rubric.md`](references/summary-rubric.md)
- [`references/detailed-review.md`](references/detailed-review.md)
- [`references/managed-collections.md`](references/managed-collections.md)

## 故障排查

### Zotero 连接被拒绝

确认 Zotero 桌面端正在运行，并检查本地 API 是否可用：

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py probe --vault .
```

### 有 PDF，但只能按摘要总结

Zotero 可能尚未完成全文索引。等待索引完成后重新同步；不要把“存在 PDF 附件”等同于“全文可读取”。

### 提示服务器标识发生变化

这通常说明当前 Zotero 数据库与保存同步状态时的数据库不同。不要直接执行重置；先确认是否切换了 Zotero profile、数据库或电脑，再决定是否使用 `bootstrap --reset`。

### Obsidian 表格没有更新

先确认笔记位于 `Zotero/Papers` 且 Properties 完整，然后重建分类视图：

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py refresh-collections --vault .
```

### Windows 报告文件被占用

Obsidian 或索引程序可能短暂读取目标笔记。脚本会跳过内容完全相同的重复覆盖；如果内容确实变化且仍然失败，等待 Obsidian 完成索引后重试。

## Skill 文件结构

```text
zotero-obsidian-literature/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
├── assets/
│   ├── config.json
│   ├── Library.base
│   └── Zotero Dashboard.md
├── references/
│   ├── detailed-review.md
│   ├── managed-collections.md
│   ├── summary-rubric.md
│   └── summary-schema.md
└── scripts/
    └── zotero_sync.py
```

## 已知边界

- 不提供无需 Codex 指令的实时后台监听。
- 不修改 Zotero 条目、标签、分类、附件或数据库。
- 不永久删除 Obsidian 文献笔记。
- 只有 Zotero 已索引并通过本地 API 提供的文本才能作为全文证据。
- 扫描、状态跟踪和渲染是确定性的；自然语言摘要会受模型能力、上下文长度和原文质量影响。

## 贡献与发布

提交变更前建议运行 Skill 校验和 Python 语法检查：

```powershell
python <skill-creator目录>/scripts/quick_validate.py .
python -m py_compile scripts/zotero_sync.py
```

如果公开发布，请先确认仓库不包含真实 Obsidian 仓库中的 `.sync` 状态、论文全文、受版权保护的 PDF 或个人笔记，并为项目选择合适的开源许可证。
