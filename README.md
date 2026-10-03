# batch_vr180_60deg

ステレオ写真（MPO）と通常の JPEG を、VR180 で「60度幅の仮想スクリーンに投影したように」見える正距円筒（エクイレクタングラー）画像へ一括変換する Python スクリプト集です。
VEGAS Pro で編集 → VR180 Creator（視野角 60° に設定）でレンダリングする前段の素材作りを想定しています。

Batch-convert stereo MPO photos and ordinary JPEGs into narrow-FOV (default 60°) equirectangular images for VR180 workflows.

## スクリプト

| | `batch_vr180_60deg/batch_vr180_60deg.py` | `batch_vr180_60deg/equirect_mono_60deg.py` |
|---|---|---|
| 入力 | ステレオ MPO（左右ペア） | 通常の単眼 JPEG |
| 出力 | MPO（2画像） | JPEG |
| 画角 | EXIF（焦点距離・焦点面解像度）から実レンズ画角を算出 | 写真の横幅 = 指定角度と仮定（縦はアスペクト比から算出） |

共通の特徴:

- キャンバスは指定画角（既定 60°）分だけ。元の解像度密度を保ちます
- EXIF を引き継ぎます（幅・高さのみ書き換え）
- JPEG 品質 100・4:4:4・ハフマン最適化
- MPO 版は MPF の Attribute IFD（基線長・輻輳角）と、画像2側の Exif/MPF も引き継ぎ、StereoPhoto Maker で開けることを確認済み
- `<名前>_<N>deg` で終わるファイルは変換済みとみなしてスキップ（二重変換防止）

## 使い方

```
pip install -r requirements.txt
python batch_vr180_60deg/batch_vr180_60deg.py --src <MPOフォルダ> --out <出力先>
python batch_vr180_60deg/equirect_mono_60deg.py --src <JPEGフォルダ> --out <出力先>
```

オプション: `--fov-canvas`（既定 60）、`--quality`（既定 100）、`--fast`（Lanczos4 の代わりにバイリニア）

出力ファイル名は `<元の名前>_60deg.MPO` / `.jpg` です。

## 動作確認状況

- FujiFilm FinePix REAL 3D W1 の MPO で動作確認、StereoPhoto Maker で読み込み確認済み
- VEGAS Pro / VR180 Creator での最終的な見え方は未検証です
- 単眼版は他機種の JPEG での広範なテストは未実施です

## ライセンス

[MIT License](LICENSE)
