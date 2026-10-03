# VR180変換ツール (batch_vr180_60deg)

## プロジェクトの目的

FujiFilm FinePix REAL 3D W1 で撮影したステレオ写真(MPOファイル)、および通常のデジタルカメラのJPEG写真を、VR180で視聴した際に「60度幅の仮想スクリーンに投影したような見た目」になる正距円筒(エクイレクタングラー)画像に変換する。

最終的な用途は VEGAS Pro での動画編集 → VR180 Creator でのレンダリング(FOV を60度に設定)。StereoPhoto Maker (`stphmkrpro_j.exe`) で開けることを互換性の判定基準にしている。

この背景は claude.ai のプロジェクト「VR180 データ作成」で Claude(Sonnet 5)と相談しながら作り込んだもの。経緯の全文は `batch_vr180_60deg/memo.txt` にある(Shift-JISで保存されたチャットの貼り付け。文字化けする場合は `iconv -f SHIFT-JIS -t UTF-8` で読む)。

## 構成

```
batch_vr180_60deg.sln                      ... Visual Studio ソリューション
batch_vr180_60deg/
  batch_vr180_60deg.pyproj                 ... VS の Python プロジェクトファイル
  batch_vr180_60deg.py                     ... ステレオ MPO 用変換スクリプト(本体)
  equirect_mono_60deg.py                   ... 単眼 JPEG 用変換スクリプト(本体)
  memo.txt                                 ... 開発時のチャットログ(Shift-JIS)
  env/                                     ... Python 3.9 venv(opencv-python, numpy, pillow 導入済み)
  test_in/, test_out/                      ... MPO 変換のテスト用 入力/出力
  test2d_in/, test2d_out/                  ... 単眼 JPEG 変換のテスト用 入力/出力
  vr180_out/                               ... 手動実行時の出力先(デフォルト)
  150914_YamatoMuseum24*.MPO/.mpo          ... 検証に使った実写データ
    - 150914_YamatoMuseum24.MPO            元のカメラファイル
    - 150914_YamatoMuseum24_mod.mpo        StereoPhoto Maker が書き出した参考ファイル(互換性検証の基準)
    - 150914_YamatoMuseum24_60deg.MPO      変換後の確認済み出力
```

**実行環境**: `env\Scripts\activate`(Windows)でこのプロジェクト専用の venv を有効化してから実行する。追加の pip install は不要(opencv-python, numpy, pillow 導入済み)。

## 2つのスクリプトの使い分け

| | `batch_vr180_60deg.py` | `equirect_mono_60deg.py` |
|---|---|---|
| 入力 | ステレオ MPO(左右ペア) | 通常の単眼 JPEG |
| 出力 | MPO コンテナ(2画像) | 単一 JPEG |
| 画角の扱い | EXIF(FocalPlaneResolution)から実際のレンズ画角を算出 | **画角判定なし**。写真の横幅=指定角度(既定60°)と単純に仮定。縦はアスペクト比から算出 |

両スクリプト共通のCLI引数: `--src`(入力フォルダ) `--out`(出力フォルダ) `--fov-canvas`(既定60) `--quality`(既定100) `--fast`(Lanczos4の代わりにbilinear、高速だが粗い)

```
python batch_vr180_60deg.py --src <MPOフォルダ> --out <出力先>
python equirect_mono_60deg.py --src <JPEGフォルダ> --out <出力先>
```

## 設計上の重要な決定(なぜこうなっているか)

- **キャンバスは180°ではなく60°**: VR180 Creator 側で視野角を60°に手動設定する前提。180°全天球にすると黒枠だらけで無駄に重くなるため、実際に使う範囲(60°)だけのキャンバスにして解像度密度を保っている
- **単眼版はEXIFベースの画角判定を意図的に撤去済み**: 当初は FocalPlaneResolution → FocalLengthIn35mmFilm の2段階フォールバックで実際の光学画角を精密に算出するロジックを実装したが、目的が「実画角の再現」ではなく「写真を仮想スクリーンとして配置すること」だと分かった時点で、単純な「横幅=指定角度」モデルに置き換えた。**実画角の判定ロジックを復活させる提案は不要**(意図的な単純化であり、退化ではない)
- **EXIF は常にバイト単位で引き継ぐ**: 元画像の Exif APP1 セグメントをそのままコピーし、幅・高さフィールドだけ新しいキャンバスサイズに書き換える。再生成ではなく既存バイト列の部分パッチなのは、カメラ機種名や撮影日時などの情報を一切欠落させないため
- **画質設定は意図的に最大化してある**(品質100・4:4:4クロマサブサンプリング・ハフマン最適化)。黒枠部分の圧縮効率が良いため出力ファイルが元より小さくなることがあるが、これは劣化ではなく正常(黒は圧縮が効きやすいだけ)
- **二重変換防止**: 両スクリプトとも、入力フォルダ内で `<名前>_<N>deg`(N = `--fov-canvas` の整数部)で終わるファイルは自分の出力とみなしてスキップする(`skip (already converted)` と表示)。変換済みファイルを再入力して `_60deg_60deg` ができるのを防ぐ。別名の出力は判定できない

## MPOコンテナに関するハマりどころ(重要)

StereoPhoto Maker で開けない問題を調査して判明した、MPO形式の仕様上の要件:

1. **画像2(右目)側にも、画像1と同様に独自のExif APP1セグメントとMPF APP2セグメントが必要**。代表画像(1枚目)だけに付与するのでは不十分で、これが「StereoPhoto Makerでファイルが開けない」の直接の原因だった
2. **MPFの「Attribute IFD」(ステレオ基線長・輻輳角の情報、FujiFilm REAL 3D W1 では基線長77mm)はバイト単位でそのまま複製する**。再投影しても物理的なカメラ構造は変わらないため、元ファイルの値をそのまま流用してよい
3. 上記の検証は `150914_YamatoMuseum24_mod.mpo`(StereoPhoto Maker自身が書き出した参考ファイル)と元のカメラファイルをバイトレベルで突き合わせて特定した。MPOの仕様書を読むより、動作する参考ファイルとの差分を取るほうが早い

この2点を両方満たさないと、Pillow では正常に読めてもStereoPhoto Makerでは「開けません」とだけ表示されて原因が分かりにくい。

## 現在の状態 / 未確認事項

- 2つのスクリプトとも動作確認済み(StereoPhoto Maker・目視確認)
- VEGAS Pro への取り込み、VR180 Creator での実際のレンダリング結果は未確認(ユーザー側で今後実施)
- `equirect_mono_60deg.py` は実機JPEGでの広範なテストはまだ(FujiFilmのMPOから抽出した疑似サンプルで確認した程度)
- Git リポジトリはこのフォルダ(`batch_vr180_60deg/`)にあり、`.gitignore` で `env/`・テスト入出力・MPO/JPG・`memo.txt` を除外済み。コミットは初回の1件のみで、`.gitignore` と `CLAUDE.md` の変更が未コミットの場合がある
- リモートリポジトリ(GitHub等)は未設定

## 関連ドキュメント

- Obsidian 作業記録: `作業まとめ保管庫/VR180変換ツール_作業記録.md`
- ブログ下書き: `作業まとめ保管庫/VR180変換ツール_ブログ下書き.md`
