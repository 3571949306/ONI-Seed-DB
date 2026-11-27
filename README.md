# ONI Community Seed Database (ONI-Seed-DB)

这是配套 **[ONI-Trainer](https://github.com/3571949306/ONI-Trainer)** 的社区开局种子共享仓库模板与自动合并框架。

每位玩家在使用修改器筛选开局种子时，修改器会在后台**全量自动记录所有评估过的种子**（无论是否命中玩家当前的筛选条件），仅保存**种子编号、完整坐标、各星球特质、间歇泉 / 火山 / 遗迹坐标与产率评级**（不含地图预览图，体积极小）。

玩家点击「上传记录」即可将待上传种子一键打包（`[seed-db-gzip]` GZip + Base64 高压缩比格式）上传至本仓库的 GitHub Issues；仓库内的 GitHub Actions 机器人会自动完成**解压校验、坏数据过滤、按星群分片去重合并**，并实时更新 `manifest.json` 索引。其他玩家拉取同步后，在「开局种子筛选」中点击开始筛选时即可直接命中内存**毫秒级快速索引**，未命中时也会自动跳过已评估过的种子。

---

## 目录结构

```text
ONI-Seed-DB/
├── manifest.json                         # 分片索引与 SHA-256 校验清单（由机器人自动维护）
├── data/
│   ├── .gitkeep
│   └── <CLUSTER_PREFIX>.json             # 按星群前缀分片存储的种子库（如 V-CER-C.json / SNDST-C.json）
├── scripts/
│   └── merge_submission.py               # 纯标准库 Python3 校验 / 去重合并 / 重建 manifest 脚本
└── .github/
    ├── ISSUE_TEMPLATE/
    │   └── seed-submission.yml           # 玩家投稿 Issue 模板（label: seed-submission）
    └── workflows/
        └── merge-seeds.yml               # 自动校验、合并提交并回复/关闭 Issue 的工作流
```

---

## 如何部署为公开社区仓库

1. 在 GitHub 上新建或 Fork 本模板仓库（例如 `你的用户名/ONI-Seed-DB`）。
2. 进入仓库 **Settings → Actions → General → Workflow permissions**，勾选 **Read and write permissions**，保存。
3. 在 `ONI-Trainer` 客户端点击「开局种子筛选 → 社区库 → 数据源设置」，填写仓库名 `owner/repo` 与分支 `main`（支持自定义镜像加速前缀）。

---

## 数据规范 (`schemaVersion: 1`)

### 投稿 JSON 格式（由 ONI-Trainer 自动生成）

```json
{
  "schemaVersion": 1,
  "seeds": [
    {
      "schemaVersion": 1,
      "clusterPrefix": "V-CER-C",
      "clusterPath": "dlc2::clusters/CeresClassicCluster",
      "clusterName": "谷神星星群",
      "seed": 123456789,
      "coordinate": "V-CER-C-123456789-0-0-0",
      "storyCode": "0",
      "mixingCode": "0",
      "dlcFlags": "EXPANSION1_ID,DLC2_ID",
      "author": "Player",
      "submittedAt": "2026-11-25T10:00:00Z",
      "traitSummary": "[初始] 谷神小行星：火山活跃、金属富足",
      "poiSummary": "谷神小行星：3×金火山(S)",
      "note": "出生点附近双 S 级金火山",
      "planets": [
        {
          "worldIndex": 0,
          "worldId": "dlc2::worlds/CeresClassicAsteroid",
          "worldName": "谷神小行星",
          "traits": ["火山活跃", "金属富足"],
          "pois": [
            {
              "prefabId": "GeyserGeneric_molten_gold",
              "displayName": "[金属火山] 金火山",
              "category": "geyser",
              "dist": 42,
              "avgYieldGps": 141.2,
              "grade": "S",
              "percentile": 94.5
            }
          ]
        }
      ]
    }
  ]
}
```

### 去重与合并策略

- 同一 `clusterPrefix` 分片内，以 `(seed, storyCode, mixingCode)` 作为唯一主键。
- 收到重复投稿时不会新增冗余条目，而是自动补全缺失的行星 POI 扫描记录、更新最新备注/贡献者列表，并重新计算 `manifest.json` 的 `count` 与 `sha256`。

---

## 本地测试

```bash
python3 scripts/merge_submission.py --repo-root . --test-json path/to/submission.json --author tester
```
