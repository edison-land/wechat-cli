"""联系人管理 — 加载、缓存、模糊匹配"""

import os
import re
import sqlite3


_contact_names = None  # {username: display_name}
_contact_full = None   # [{username, nick_name, remark}]
_self_username = None
_chatroom_nicknames = {}  # {chatroom_username: {member_username: 群昵称}}


def _read_varint(buf, index):
    """读取一个 protobuf varint，越界或超长时返回 (None, len(buf))。"""
    result = shift = 0
    while index < len(buf):
        byte = buf[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            break
    return None, len(buf)


def _iter_protobuf_fields(buf):
    """遍历 protobuf 顶层字段，产出 (字段号, 值)。遇到无法识别的内容就停止。"""
    index = 0
    while index < len(buf):
        tag, index = _read_varint(buf, index)
        if tag is None:
            return
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, index = _read_varint(buf, index)
            if value is None:
                return
            yield field, value
        elif wire == 2:
            length, index = _read_varint(buf, index)
            if length is None or index + length > len(buf):
                return
            yield field, buf[index:index + length]
            index += length
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            return


def _parse_chatroom_nicknames(buf):
    """从 chat_room.ext_buffer 解析出 {成员 username: 群昵称}。

    每条成员记录是顶层字段 1 的子消息，其中子字段 1 是 username、子字段 2 是群昵称。
    没设群昵称的成员不会有子字段 2，直接跳过，让调用方回退到通讯录名称。
    """
    nicknames = {}
    for field, value in _iter_protobuf_fields(buf or b""):
        if field != 1 or not isinstance(value, bytes):
            continue
        username = nickname = None
        for sub_field, sub_value in _iter_protobuf_fields(value):
            if not isinstance(sub_value, bytes):
                continue
            if sub_field == 1:
                username = sub_value
            elif sub_field == 2:
                nickname = sub_value
        if username and nickname:
            nicknames[username.decode("utf-8", "ignore")] = nickname.decode("utf-8", "ignore")
    return nicknames


def get_group_display_names(chatroom_username, cache, decrypted_dir):
    """群聊中成员的对外称呼：群昵称 > 对方的微信昵称 > username。

    刻意不使用通讯录备注——那是阅读者本人给联系人起的名字，属于个人设置，
    不应出现在面向整个群的输出里。
    """
    names = {}
    for contact in get_contact_full(cache, decrypted_dir):
        # 每个联系人都要显式覆盖，否则没有昵称的人会穿透到备注优先的映射。
        names[contact['username']] = contact['nick_name'] or contact['username']
    names.update(get_chatroom_nicknames(chatroom_username, cache, decrypted_dir))
    return names


def get_chatroom_nicknames(chatroom_username, cache, decrypted_dir):
    """群昵称映射；群聊里它比通讯录备注更贴近群成员看到的称呼。"""
    if chatroom_username in _chatroom_nicknames:
        return _chatroom_nicknames[chatroom_username]

    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    db_path = pre_decrypted if os.path.exists(pre_decrypted) else cache.get(
        os.path.join("contact", "contact.db")
    )
    nicknames = {}
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT ext_buffer FROM chat_room WHERE username = ?", (chatroom_username,)
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                nicknames = _parse_chatroom_nicknames(row[0])
        except sqlite3.Error:
            nicknames = {}

    _chatroom_nicknames[chatroom_username] = nicknames
    return nicknames


def _load_contacts_from(db_path):
    names = {}
    full = []
    conn = sqlite3.connect(db_path)
    try:
        for r in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            uname, nick, remark = r
            display = remark if remark else nick if nick else uname
            names[uname] = display
            full.append({'username': uname, 'nick_name': nick or '', 'remark': remark or ''})
    finally:
        conn.close()
    return names, full


def get_contact_names(cache, decrypted_dir):
    global _contact_names, _contact_full
    if _contact_names is not None:
        return _contact_names

    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        try:
            _contact_names, _contact_full = _load_contacts_from(pre_decrypted)
            return _contact_names
        except Exception:
            pass

    path = cache.get(os.path.join("contact", "contact.db"))
    if path:
        try:
            _contact_names, _contact_full = _load_contacts_from(path)
            return _contact_names
        except Exception:
            pass

    return {}


def get_contact_full(cache, decrypted_dir):
    global _contact_full
    if _contact_full is None:
        get_contact_names(cache, decrypted_dir)
    return _contact_full or []


def resolve_username(chat_name, cache, decrypted_dir):
    names = get_contact_names(cache, decrypted_dir)
    if chat_name in names or chat_name.startswith('wxid_') or '@chatroom' in chat_name:
        return chat_name
    chat_lower = chat_name.lower()
    for uname, display in names.items():
        if chat_lower == display.lower():
            return uname
    for uname, display in names.items():
        if chat_lower in display.lower():
            return uname
    return None


def get_self_username(db_dir, cache, decrypted_dir):
    global _self_username
    if _self_username:
        return _self_username
    if not db_dir:
        return ''
    names = get_contact_names(cache, decrypted_dir)
    account_dir = os.path.basename(os.path.dirname(db_dir))
    candidates = [account_dir]
    m = re.fullmatch(r'(.+)_([0-9a-fA-F]{4,})', account_dir)
    if m:
        candidates.insert(0, m.group(1))
    for candidate in candidates:
        if candidate and candidate in names:
            _self_username = candidate
            return _self_username
    return ''


def get_group_members(chatroom_username, cache, decrypted_dir):
    """获取群聊成员列表。

    通过 contact.db 的 chatroom_member 关联表查询。

    Returns:
        dict: {'members': [...], 'owner': str}
        每个 member: {'username': ..., 'nick_name': ..., 'remark': ..., 'display_name': ...}
    """
    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        db_path = pre_decrypted
    else:
        db_path = cache.get(os.path.join("contact", "contact.db"))

    if not db_path:
        return {'members': [], 'owner': ''}

    names = get_contact_names(cache, decrypted_dir)
    conn = sqlite3.connect(db_path)
    try:
        # 1. 找到 chatroom 的 contact.id
        row = conn.execute("SELECT id FROM contact WHERE username = ?", (chatroom_username,)).fetchone()
        if not row:
            return {'members': [], 'owner': ''}
        room_id = row[0]

        # 2. 获取群主
        owner = ''
        owner_row = conn.execute("SELECT owner FROM chat_room WHERE id = ?", (room_id,)).fetchone()
        if owner_row and owner_row[0]:
            owner = names.get(owner_row[0], owner_row[0])

        # 3. 获取成员 ID 列表
        member_ids = [r[0] for r in conn.execute(
            "SELECT member_id FROM chatroom_member WHERE room_id = ?", (room_id,)
        ).fetchall()]
        if not member_ids:
            return {'members': [], 'owner': owner}

        # 4. 批量查询成员信息
        placeholders = ','.join('?' * len(member_ids))
        members = []
        for uid, username, nick, remark in conn.execute(
            f"SELECT id, username, nick_name, remark FROM contact WHERE id IN ({placeholders})",
            member_ids
        ):
            display = remark if remark else nick if nick else username
            members.append({
                'username': username,
                'nick_name': nick or '',
                'remark': remark or '',
                'display_name': display,
            })

        # 按 display_name 排序，群主排最前
        members.sort(key=lambda m: (0 if m['username'] == (owner_row[0] if owner_row else '') else 1, m['display_name']))

        return {'members': members, 'owner': owner}
    finally:
        conn.close()


def get_contact_detail(username, cache, decrypted_dir):
    """获取联系人详情。

    Returns:
        dict or None: 联系人详细信息
    """
    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        db_path = pre_decrypted
    else:
        db_path = cache.get(os.path.join("contact", "contact.db"))
    if not db_path:
        return None

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT username, nick_name, remark, alias, description, "
            "small_head_url, big_head_url, verify_flag, local_type "
            "FROM contact WHERE username = ?",
            (username,)
        ).fetchone()
        if not row:
            return None
        uname, nick, remark, alias, desc, small_url, big_url, verify, ltype = row
        return {
            'username': uname,
            'nick_name': nick or '',
            'remark': remark or '',
            'alias': alias or '',
            'description': desc or '',
            'avatar': small_url or big_url or '',
            'verify_flag': verify or 0,
            'local_type': ltype,
            'is_group': '@chatroom' in uname,
            'is_subscription': uname.startswith('gh_'),
        }
    finally:
        conn.close()


def display_name_for_username(username, names, db_dir, cache, decrypted_dir):
    if not username:
        return ''
    if username == get_self_username(db_dir, cache, decrypted_dir):
        return 'me'
    return names.get(username, username)
