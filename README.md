# OSM Building Post-processing

根据原始 OSM 范围自动下载 Esri World Imagery，并将使用洋红色（`#FF00FF`）描好建筑边界的图片转换为可在 JOSM 中人工复核的 OSM 文件。

本工具包负责下载卫星图和处理描边完成后的图片，但**不负责**自动识别建筑或上传 OpenStreetMap。

## 功能

- 从洋红色（`#FF00FF`）闭合描线提取建筑轮廓；
- 删除直线上的冗余节点；
- 建筑边角正交化；
- 仅在局部邻域内统一相近建筑方向；
- 排除图片边缘的不完整建筑；
- 合并重叠图片分块并去除重复、包含和嵌套对象；
- 对朝向相近的轻度重叠矩形进行保守缩边；
- 输出 GeoJSON、OSM XML 和结构校验报告；
- 不修改输入 OSM，不自动上传。

## 环境

- Python 3.11 或更高版本；
- Pillow；
- NumPy；
- JOSM，仅用于最终人工复核。

安装依赖：

```bash
python -m pip install -r requirements.txt
```

## 图形界面（推荐）

Windows 用户直接双击 `启动工具.bat`。它会查找可用的 Python（可使用系统 Python，也可使用 Codex 附带的 Python），并自动检查、安装所需组件。这个工具本身不依赖 Codex。如果启动失败，窗口会保留错误信息。也可以运行：

```bash
python gui.py
```

图形界面只处理**一张描边图片**，无需理解瓦片编号：

1. 选择原始 `.osm` 文件和工作文件夹；
2. 点击“下载卫星图”，程序按 OSM 范围自动下载并拼接 18 级影像；
3. 使用任意图片编辑工具，在 `其他文件/esri-imagery-z18.jpg` 上以洋红色（`#FF00FF`）描出闭合建筑边界；不能裁剪或改变宽高比，等比例缩放可以接受；
4. 在 GUI 中选择描边后的图片，点击“转换为 OSM”；
5. 转换结束后，用 JOSM 打开输出文件夹顶层的 `最终结果.osm`。

下载时会在工作文件夹的 `其他文件` 子目录生成 `imagery-info.json`，请勿删除；它保存像素与经纬度转换所需的信息。GUI 使用推荐处理参数，并将本次运行配置保存在同一子目录。关闭窗口不会上传任何数据。

为避免意外下载过大范围，GUI 一次最多下载 64 张瓦片。范围过大时，请在 JOSM 中重新下载一个更小的区域。影像来源信息与下载时间会写入 `imagery-info.json`。

命令行入口仍支持通过 manifest 一次处理多张重叠图片，供高级用户使用。

## 输入要求

每张图片必须对应一个连续的 XYZ 瓦片矩形，并已用洋红色（`#FF00FF`）线条描出完整建筑：

- 描线颜色：洋红色（`#FF00FF`）；
- 轮廓必须闭合；
- 不要填充建筑内部；
- 图片不能在描边后裁剪、旋转或改变宽高比；
- 必须知道左上角瓦片的 `x/y/z`；
- 必须知道拼图包含多少列和多少行瓦片；
- 相邻分块应保留重叠区，避免分块边缘建筑遗漏。

## 配置

复制 `manifest.example.json`，例如：

```bash
copy manifest.example.json manifest.json
```

相对路径均相对于 manifest 文件所在目录解析。每个 `chunk` 的字段：

- `name`：唯一名称；
- `image`：描边图片路径；
- `x`、`y`：左上角 XYZ 瓦片编号；
- `z`：缩放层级；
- `tiles_wide`、`tiles_high`：拼图瓦片列数和行数。

方向调整默认规则：

- 邻域半径120个输出像素；
- 原始角度差不超过8°；
- 至少3栋相近建筑才共享局部方向；
- 孤立或方向不匹配的建筑保留自身方向。

## 运行

```bash
python run_postprocess.py manifest.json
```

主要输出：

```text
output/area/最终结果.osm
output/area/原始文件-area.osm
output/area/其他文件/buildings-final.geojson
output/area/其他文件/validation.json
```

使用 JOSM 打开 `最终结果.osm`，叠加原影像并运行 JOSM Validator。必须逐栋检查后再决定是否上传。

## 单独运行

### 矢量化一个分块

```bash
python vectorize_outlines.py outlined.png \
  --x 214028 --y 107604 --z 18 \
  --tiles-wide 6 --tiles-high 4 \
  --color-mode strict \
  --orientation-radius 120 \
  --orientation-tolerance 8 \
  --geojson chunk.geojson \
  --osm chunk.osm
```

### 验证 OSM

```bash
python validate_osm.py output/area/最终结果.osm
```

## 安全边界

- 输出仅是候选数据；
- 不根据图片边缘处的不完整屋顶生成建筑；
- 不自动处理大面积重叠或方向明显不同的建筑；
- 不自动上传；
- 使用者必须确认影像许可、坐标系、影像偏移和署名要求；
- 大范围系统性编辑必须遵守当地社区流程及 OSM 自动编辑/导入规范。
