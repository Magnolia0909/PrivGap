# PrivGap

The main entry point is `main.py`.

## Prerequisites

We recommend Linux, Python 3.10 or later, Node.js 18 or later, and an SSD-backed workspace.

```bash
python3 --version
node --version
npm --version
```

Create a conda environment and install the required packages:

```bash
conda create -n privgap python=3.10
conda activate privgap
pip install requests urllib3 coloredlogs json-repair torch transformers sentence-transformers sentencepiece
```

## Repository Layout

```text
config/                 Runtime configuration
data/                   Loaders and shared data
extractor/              Guideline and policy extraction
flow_analysis/          Static mini-app data-flow analysis
utils/                  Shared IO and text helpers
main.py                 Entry point
```

### Download Guidelines

For reproducing the guideline annotation process, WeChat mini-app privacy
guidelines can be downloaded from the following public interface:

```text
https://mp.weixin.qq.com/wxawap/waprivacyinfo?appid=xxxxxxx&action=show
```

Replace `xxxxxxx` with the target WeChat mini-app app id. The downloaded
page can then be inspected and converted into the annotated privacy-item format
shown above.

### Unpack mini-app Code

PrivGap runs static analysis on unpacked mini-app source code. For WeChat, we used `wedecode` to unpack `.wxapkg` packages. For Douyin/TikTok, we used `ttpkgUnpacker` to unpack the packages. For Alipay, the packages can be unpacked directly.

## Configuration

Edit `config/config.py` or use environment variables.

Select a platform:

```bash
export SOURCE=wechat
```

Supported values are `wechat`, `alipay` and `tiktok`.

The public artifact does not contain real API keys. Replace the placeholder values in `config/config.py`, or provide credentials through environment variables:

```bash
export LLM_BACKEND=model-name
export LLM_API_KEY=replace-with-your-api-key
export LLM_API=replace-with-your-api-base-url
export LLM_MODEL_NAME=replace-with-your-model-name
```

## Usage

Run the complete pipeline:

```bash
python3 main.py
```

Run one mini-app:

```bash
python3 main.py --app-id <APP_ID>
```

Run another platform:

```bash
SOURCE=alipay python3 main.py
SOURCE=tiktok python3 main.py
```
