https://github.com/akomekagome/SmileMusic に自分が欲しい機能を追加しています。

# 追加した機能
* 自動退出
  * 参加しているVCが自分以外退出orBOTのみになったら退出するように 
* Youtubeのリスト(pl,pld)
  * APIを使い各動画のidを取得してキューに入れている
* Youtubeのライブ(live)
  * とりあえず再生時間を1秒にして渡しているだけ 
* ストリーム再生のオンオフ(set_stream)
  * ダウンロードしてから再生出来るようにした 
* ストリーム再生の確認(info_stream)
  * 上記設定の確認 
* opusに圧縮して再生(pd,pld)
  *  FFmpegOpusAudioで再生するように
# 主な変更点
* Pythonのバージョン変更(3.9.12)
  * なんか古いのが嫌だったから
* ytdlをytdlpに変更
  * 更新が停止しているし一部動画が再生できないため
* libopusが含まれないのでffmpegを自前でビルドするように
  * ffmpegをaptでゲットすると使いたいlibopusが無いため
* ytdlのバッファサイズを16Kに
  * 当環境だとたまに不安定になるため

なんか問題など有りましたらTwitter等に連絡をください。
