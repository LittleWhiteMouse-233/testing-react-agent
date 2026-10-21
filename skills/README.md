# Skills 与静态资源

宿主直接读取本目录，不将本地 Skill 包装为 MCP。`TEST_AGENT_SKILLS_DIR` 可指定其它根目录，相对路径基于仓库根目录。

- `base/`：随应用版本维护的 Skill。
- `external/`：用户放入的外部 Skill，内容不进入 Git。

每个包包含 `SKILL.md`，所在目录及其所有子目录是包内资源。主文件使用 YAML frontmatter 提供 `name` 和 `description`：

```markdown
---
name: example
description: 说明何时需要使用此 Skill
---
使用 [参考文档](references/guide.md)，需要时查看 ![参考图片](assets/example.png)。
```

初始上下文只提供没有祖先 Skill 的顶层摘要；嵌套 Skill 通过父包资源列表或正文引用按需访问。列父包时子 SKILL.md 与附件均是普通资源；以子入口 URI 查询可限定到子包。

三个工具统一使用 `source_id` 和 URI：本地来源为 `local`，MCP 来源为 `mcp:<配置名称>`。`load_skill` 只读取主文件；`list_resources` 返回目录 JSON 文本；`read_resource` 读取文本或图片。相对引用必须指定 `base_uri`，推荐使用当前文档的完整 URI；目录基础 URI 需以 `/` 结尾。已知不支持的 MIME 直接返回说明，不读取本地字节或发起远端读取。不会执行 scripts 或自动读取附件。

MCP 包采用同一来源、同一 scheme/authority 下的 URI 路径范围，无需对应服务端物理目录。只有 `skill` scheme 且文件名为 `SKILL.md` 的入口自动归为 Skill；其它静态资源仍可查询和读取。模板未枚举的资源可以按已知 URI 读取；FastMCP 的已暴露 manifest 用于枚举附件，普通模板不意味着可浏览服务端文件系统。

运行快照保存发现时的目录元数据，列表不重新扫描本地文件；已暴露 manifest 仅在查询时展开，不回写目录。列表依次按来源筛选、展开顶层 manifest、合并且优先保留原条目，最后按 Skill 包筛选。全文在调用时读取，成功加载的指令仅在当前 TaskRun 保留。图片与文本均为普通运行数据；图片统一存储并按图片窗口回收，不强制用于判定。修改文件会影响之后的动态读取，历史快照不是完整内容归档。各包通过自身 SKILL.md 说明用途，本目录只维护这一份主 README。
