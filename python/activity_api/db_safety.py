"""共有 psycopg2 接続の自己修復ヘルパ。

このアプリは単一のグローバル psycopg2 接続を全スレッドで共有し、
playlist_store.db_lock で直列化している。psycopg2 の既定 (非 autocommit)
では一度クエリが失敗するとトランザクションが aborted のまま残り、
rollback されない限り以後の全クエリが InFailedSqlTransaction で失敗する
(2026-07-06 のライブラリ読込不能障害の構造原因)。

ここでは接続を「呼び出し単位で必ずクリーンな状態に戻す」ための部品を提供する:

- ensure_clean_txn:  実行前に前回の残骸 (aborted / 開きっぱなし) を rollback
- rollback_quietly:  例外時の best-effort rollback (失敗してもログのみ)
- end_open_txn:      読み取り専用関数が開いたままにしたトランザクションを閉じ、
                     idle-in-transaction を残さない
- ManagedConnection: 切断された接続を次回利用時に自動再接続するプロキシ

いずれも db_lock 保持中に呼ばれる前提 (並行アクセスはロックが排除する)。
"""
from __future__ import annotations

import logging

import psycopg2.extensions as _ext

log = logging.getLogger(__name__)


def rollback_quietly(conn) -> None:
    """best-effort rollback。接続断などで失敗しても例外は伝播させない。"""
    try:
        conn.rollback()
    except Exception:
        log.warning("rollback failed (connection likely lost)", exc_info=True)


def ensure_clean_txn(conn) -> None:
    """DB 呼び出しの前処理: 前回の呼び出しが残したトランザクションを掃除する。

    - INERROR: 以前のクエリ失敗で aborted のまま → rollback (警告ログ付き。
      このログが出たら「直前に何かのクエリが失敗して未 rollback だった」印)
    - INTRANS: 読み取りが開いたまま → rollback (読み取りしか残らない設計。
      書き込み関数は返る前に必ず commit する)
    """
    try:
        status = conn.info.transaction_status
    except Exception:
        return  # 接続が壊れている場合は実行時のエラーに任せる
    if status == _ext.TRANSACTION_STATUS_INERROR:
        log.warning(
            "aborted transaction が残っていたため rollback しました "
            "(直前の DB エラーが未 rollback)"
        )
        rollback_quietly(conn)
    elif status == _ext.TRANSACTION_STATUS_INTRANS:
        rollback_quietly(conn)


def end_open_txn(conn) -> None:
    """DB 呼び出しの後処理: 読み取りが開いたトランザクションを閉じる。

    書き込み関数は commit 済みなので IDLE。IDLE 以外 (INTRANS 等) なら
    読み取り専用の呼び出しだったということなので rollback で閉じ、
    接続を idle-in-transaction のまま放置しない。
    """
    try:
        status = conn.info.transaction_status
    except Exception:
        return
    if status != _ext.TRANSACTION_STATUS_IDLE:
        rollback_quietly(conn)


class ManagedConnection:
    """単一共有 psycopg2 接続のプロキシ。切断されたら次の利用時に再接続する。

    既存コードが使う接続 API (cursor / commit / rollback / info / closed) のみ
    公開する。生成時は従来どおり即接続する (DB 未起動なら起動時に fail-fast)。
    再接続判定・接続はすべて db_lock 保持中に行われる前提。
    """

    def __init__(self, factory):
        self._factory = factory
        self._conn = factory()

    def _ensure(self):
        if self._conn is None or self._conn.closed:
            log.warning("DB connection lost — reconnecting")
            self._conn = self._factory()
        return self._conn

    def cursor(self, *args, **kwargs):
        return self._ensure().cursor(*args, **kwargs)

    def commit(self) -> None:
        conn = self._conn
        if conn is None:
            return
        # closed でも psycopg2 に InterfaceError を送出させる。黙って no-op に
        # すると「INSERT 実行後・commit 前に切断」の書き込み消失が成功として
        # 報告されてしまう (rollback と違い、commit の失敗は呼び出し元が知るべき)。
        conn.commit()

    def rollback(self) -> None:
        conn = self._conn
        if conn is None or conn.closed:
            return
        conn.rollback()

    @property
    def info(self):
        return self._ensure().info

    @property
    def closed(self) -> int:
        return self._conn.closed if self._conn is not None else 1
