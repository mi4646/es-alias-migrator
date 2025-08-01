# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import logging
import argparse
import warnings
from typing import Optional
from urllib3.exceptions import InsecureRequestWarning

# Suppress SSL-related warnings
warnings.simplefilter('ignore', InsecureRequestWarning)
warnings.filterwarnings("ignore", message=".*verify_certs=False.*")

from elasticsearch import exceptions
from elasticsearch import Elasticsearch
from elasticsearch_dsl.connections import connections

# Connect to Elasticsearch cluster
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))))
from service.utils.config import config

es = connections.create_connection(
    hosts=config.db_hosts,
    http_auth=(config.db_user, config.db_pwd),
    verify_certs=config.db_verify_certs,
    timeout=config.db_timeout,
    max_retries=config.db_max_retries,
    retry_on_timeout=config.db_retry_on_timeout
)

# 配置日志格式和级别
logging.basicConfig(
    level=logging.INFO,  # 或 WARNING / DEBUG
    format='[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def estimate_reindex_params(doc_count):
    """
    单个索引估算 reindex 任务的参数
    :param doc_count: 索引文档总数
    :return: (slices, timeout, poll_interval)

    | 文档数范围      | slices | timeout (s) | poll_interval (s) | 说明                              |
    | ------------- | ------ | ----------- | ------------------ | ------------------------------- |
    | 1000万 - 5000万 | 12~16 | 7200        | 20~30             | 多分片并发复制，但不要太激进          |
    | 5000万 - 1亿    | 16~32 | 10800       | 30~60             | 建议开启 throttle，控制速率              |
    | > 1亿          | 分步迁移   | 单独分批   | 手动调度任务        | 不建议一次性 reindex，全量迁移容易失败或对集群造成压力 |

    """
    if doc_count < 100_000:
        return 1, 300, 5
    elif doc_count < 1_000_000:
        return 4, 600, 10
    elif doc_count < 5_000_000:
        return 8, 1800, 15
    elif doc_count < 50_000_000:
        return 16, 7200, 30
    elif doc_count < 100_000_000:
        return 32, 10800, 45
    else:
        # 超大数据集建议人工干预或分批处理
        logger.warning("超大数据集建议人工干预或分批处理，>1亿文档 建议 [分批迁移 + 异步追踪]")
        return 0, 0, 0


def get_doc_count(index_name):
    """
    :param index_name: 索引名称
    :return: 索引文档总数
    """
    try:
        return es.count(index=index_name)['count']
    except Exception as e:
        logger.exception(f"[错误] 获取索引 {index_name} 文档数失败: {e}")
        return 0


def check_index_exists(index_name):
    """
    检查索引是否存在
    :param index_name: 索引名称
    :return: 存在返回 True，否则返回 False
    """
    try:
        return es.indices.exists(index=index_name)
    except Exception as e:
        logger.exception(f"[错误] 检查索引 {index_name} 失败: {e}")
        return False


def start_reindex_async(source_index, dest_index, slices=1):
    """
    :param source_index: 源索引名称
    :param dest_index: 目标索引名称
    :param slices: 分片数，默认1，即不启用分片并发复制
    :return: 任务ID

    slices=N（N>1），Elasticsearch 会自动将任务拆分为多个子任务
    """
    body = {
        "source": {"index": source_index},
        "dest": {"index": dest_index}
    }

    try:
        response = es.reindex(
            body=body,
            wait_for_completion=False,
            request_timeout=60,
            slices=slices
        )
        task_id = response.get("task")
        logger.info(f"[信息] 已启动 reindex 任务，任务ID: {task_id}")
        return task_id
    except Exception as e:
        logger.exception(f"[错误] 启动 reindex 失败: {e}")
        return None


def track_task_status(task_id, timeout=3600, poll_interval=10):
    """
    追踪任务进度
    :param task_id: 任务ID
    :param timeout: 超时时间（秒），默认1小时
    :param poll_interval: 轮询间隔（秒），默认10秒
    :return: 任务完成返回 True，超时或失败返回 False
    """

    def get_reindex_slice_status(parent_task_id):
        """
        获取子任务进度
        :param parent_task_id: 父任务ID
        """
        try:
            tasks = es.tasks.list(detailed=True, actions="*reindex")
            reindex_tasks = tasks.get("nodes", {})

            total_created = 0
            total_total = 0
            found = False

            for node_id, node_data in reindex_tasks.items():
                logger.debug(f"node: {node_id}, task: {node_data}")
                for sub_task_id, task_info in node_data.get("tasks", {}).items():
                    if task_info.get("parent_task_id") == parent_task_id:
                        found = True
                        status = task_info.get("status", {})
                        created = status.get("created", 0)
                        total = status.get("total", 0)
                        logger.info(f"  [子任务 {sub_task_id}] 已复制 {created}/{total}")
                        total_created += created
                        total_total += total

            if found:
                logger.info(f"[汇总进度] 总计 {total_created}/{total_total} 文档已复制")
            else:
                logger.info("[信息] 暂未发现子任务（可能刚启动或已完成）")

        except Exception as e:
            logger.exception(f"[错误] 获取子任务状态失败: {e}")

    elapsed = 0
    while elapsed < timeout:
        try:
            result = es.tasks.get(task_id=task_id)
            if result.get("completed"):
                if "error" in result:
                    logger.error(f"[失败] 任务 {task_id} 出错: {result['error']}")
                    return False
                logger.info(f"[成功] 任务 {task_id} 已完成 ")
                return True
            else:
                logger.info(f"[进行中] 正在迁移数据（任务ID: {task_id}）...")
                get_reindex_slice_status(task_id)  # 追踪子任务进度

        except Exception as e:
            logger.exception(f"[错误] 查询任务状态失败: {e}")
            return False

        time.sleep(poll_interval)
        elapsed += poll_interval

    logger.error(f"[超时] 任务 {task_id} 未在 {timeout}s 内完成 ")
    return False


def reindex_data_async(source_index, dest_index):
    """
    异步迁移数据
    :param source_index: 源索引名称
    :param dest_index: 目标索引名称
    :return: 成功返回 True，失败返回 False
    """
    doc_count = get_doc_count(source_index)
    if doc_count == 0:
        logger.warning(f"[警告] 索引 {source_index} 没有文档，无需迁移")
        return True

    slices, timeout, poll_interval = estimate_reindex_params(doc_count)

    logger.info(
        f"[信息] 文档总数: {doc_count}, 自动配置: slices={slices}, timeout={timeout}s, poll_interval={poll_interval}s"
    )

    if slices == 0:
        logger.error("文档数超过1亿，建议手动分批迁移，不支持自动 reindex")
        return False

    task_id = start_reindex_async(source_index, dest_index, slices=slices)
    if not task_id:
        return False
    return track_task_status(task_id, timeout=timeout, poll_interval=poll_interval)


def rename_index_if_needed(name, mapping, suffix):
    """
    检查当前名称是否是 alias，如果当前名称是索引（非 alias），将其重命名为 {name}_{suffix}（通过异步 reindex）
    :param name: 索引名称或别名
    :param mapping: 映射配置
    :param suffix: 重命名旧索引后缀
    """
    try:
        # 是 alias 会抛异常
        es.indices.get_alias(name=name)
        logger.warning(f"[信息] {name} 是 alias，不需要重命名")
        return None
    except exceptions.NotFoundError:
        # 不是 alias，可能是 index
        if check_index_exists(name):
            new_name = name + suffix
            if check_index_exists(new_name):
                logger.warning(f"[警告] 重命名目标索引 {new_name} 已存在，请手动处理")
                sys.exit(1)

            if not create_new_index(new_name, mapping):
                logger.error(f"[错误] 创建新索引{new_name}失败，终止重命名")
                sys.exit(1)

            logger.info(f"[信息] 将索引 {name} 重命名为 {new_name}（通过异步 reindex）")
            success = reindex_data_async(name, new_name)
            if not success:
                logger.error("[错误] 异步 reindex 失败，终止重命名")
                sys.exit(1)

            es.indices.delete(index=name)
            logger.info(f"[成功] 删除原始索引 {name}")


def read_mapping_from_file(file_path):
    """
    从 JSON 文件中读取映射数据
    :param file_path: JSON 文件路径
    :return: 映射数据字典
    """
    try:
        with open(file_path, 'r') as f:
            mapping = json.load(f)
        return mapping
    except Exception as e:
        logger.exception(f"无法读取Mapping映射文件: {e}")
        return None


def create_new_index(new_index, mapping):
    """
    创建新索引并应用从文件中读取的映射
    :param new_index: 新索引名称
    :param mapping: 映射数据字典
    :return: 成功返回 True，失败返回 False
    """
    try:
        es.indices.create(index=new_index, mappings=mapping)
        logger.info(f"[成功] 创建新索引 {new_index}")
        return True
    except exceptions.RequestError as e:
        logger.exception(f"[错误] 创建新索引失败: {e}")
        return False


def update_alias_to_new_index(alias, new_index):
    """
    更新 alias 绑定到新索引，并删除旧索引的绑定
    :param alias: 别名名称
    :param new_index: 新索引名称
    :return: 成功返回 True，失败返回 False
    """
    old_indices = []

    try:
        existing = es.indices.get_alias(name=alias)
        old_indices = list(existing.keys())
        logger.info(f"[信息] alias '{alias}' 当前绑定索引: {old_indices}")
    except exceptions.NotFoundError:
        logger.warning(f"[警告] alias '{alias}' 当前未绑定任何索引")

    actions = [{"remove": {"index": i, "alias": alias}} for i in old_indices]
    actions.append({"add": {"index": new_index, "alias": alias, "is_write_index": True}})

    try:
        # es.indices.update_aliases({"actions": actions})
        es.indices.update_aliases(body={"actions": actions})
        logger.info(
            f"[成功] 索引别名为 {alias} 已从旧索引 {alias if not old_indices else old_indices} 切换到新索引 {new_index}")
        return True
    except Exception as e:
        logger.exception(f"[错误] 更新 alias 失败: {e}")
        return False


def get_latest_index_by_alias(es_conn: Elasticsearch, alias_name: str, mapping, suffix) -> Optional[str]:
    """
    获取指定别名绑定的最新索引，并返回该索引的文档数量

    :param es_conn: Elasticsearch 客户端实例
    :param alias_name: 别名名称
    :param mapping: 映射配置
    :param suffix: 重命名旧索引后缀
    :return: 成功时返回 (最新索引名称, 文档数量)，失败返回 (None, None)
    """
    try:
        # 检查别名是否存在
        if not es_conn.indices.exists_alias(name=alias_name):
            logger.warning(f"索引别名 '{alias_name}' 不存在")
            rename_index_if_needed(alias_name, mapping, suffix)
            return alias_name + suffix

        # 获取所有绑定到该别名的索引
        response = es_conn.indices.get_alias(index="*", name=alias_name)
        index_names = list(response.keys())
        logger.info(f"索引别名 '{alias_name}' 绑定的索引列表为：{index_names}")

        if not index_names:
            logger.info(f"没有索引与别名：'{alias_name}'")
            return alias_name

        # 获取每个索引的元信息（包含创建时间戳）
        index_metadata = es_conn.indices.get(index=",".join(index_names))

        # 提取创建时间（creation_date）
        index_creation_times = {
            idx: index_metadata[idx]['settings']['index']['creation_date']
            for idx in index_names
        }

        # 按创建时间排序（creation_date 是毫秒级时间戳），取最新的
        sorted_indices = sorted(index_creation_times.items(), key=lambda x: int(x[1]), reverse=True)
        logger.info(f"用创建时间排序索引：{sorted_indices}")
        latest_index = sorted_indices[0][0]

        # 获取该索引的文档数量
        stats = es_conn.indices.stats(index=latest_index)
        doc_count = stats['indices'][latest_index]['primaries']['docs']['count']
        logger.info(f"最新索引 '{latest_index}' 的文档数量为：{doc_count}")

        return latest_index

    except Exception as e:
        logger.exception(f"未能获得最新索引：{e}")
        return None


def reindex_data(old_index, new_index):
    """
    使用 reindex API 将数据从旧索引迁移到新索引
    :param old_index: 旧索引名称
    :param new_index: 新索引名称
    :return: 成功返回 True，失败返回 False
    """
    reindex_body = {
        "source": {
            "index": old_index  # Old index name
        },
        "dest": {
            "index": new_index  # New index name
        }
    }

    try:
        # 执行 reindex 操作
        es.reindex(body=reindex_body)
        print(f"数据已从{old_index} 迁移到 {new_index}.")
    except exceptions.RequestError as e:
        print(f"数据迁移失败: {e}")
        return False
    return True


# 4. 删除旧索引
def delete_old_index(old_index):
    """
    删除旧索引
    :param old_index: 旧索引名称
    :return: 成功返回 True，失败返回 False
    """
    try:
        es.indices.delete(index=old_index)
        logger.info(f"旧索引{old_index}已删除.")
    except exceptions.NotFoundError:
        logger.exception(f"[错误] 旧索引{old_index}不存在.")
        return False
    except exceptions.RequestError as e:
        logger.exception(f"[错误] 无法删除旧索引: {e}")
        return False
    except Exception as e:
        logger.exception(f"[错误] 删除旧索引时发生未知错误: {e}")
        return False
    return True


def parse_arguments():
    """
    解析命令行参数
    """
    script_name = os.path.basename(sys.argv[0])

    epilog_example = (
        f"示例:\n"
        f"  python {script_name} -o old_index -n new_index -a my_alias\n"
        f"  python {script_name} -o old_index -n new_index -a my_alias -m mapping.json\n"
        f"  python {script_name} -o old_index -n new_index -a my_alias -M '{{\"properties\": {{\"name\": {{\"type\": \"text\"}}}}}}'\n"

        f"\n"
        f"  # 示例命令 1: 使用映射文件完整调用\n"
        f"  python3 /var/www/rc-api-server/install/migrate_index.py \\\n"
        f"    --old_index rc_android_attack \\\n"
        f"    --new_index rc_android_attack_$(date +%s) \\\n"
        f"    --alias rc_android_attack \\\n"
        f"    --mapping_file_path /var/www/rc-api-server/install/mapping.json\n"
        f"\n"
        f"  # 示例命令 2: 仅使用别名自动处理索引和默认映射\n"
        f"  python3 /var/www/rc-api-server/install/migrate_index.py --alias rc_android_attack\n"
    )

    parser = argparse.ArgumentParser(
        add_help=False,
        epilog=epilog_example,
        formatter_class=argparse.RawTextHelpFormatter,
        description="Elasticsearch索引迁移工具,支持Elasticsearch 7.10+",

    )
    parser.add_argument("-h", "--help", action="help", help="显示帮助信息", default=argparse.SUPPRESS)
    parser.add_argument("-v", '--version', action='version', help="显示版本信息", version='1.2.0')

    parser.add_argument("-o", "--old_index", type=str, required=False,
                        help="指定旧索引名称。如果未指定，则自动从Elasticsearch中查找当前别名指向的最新索引。")

    parser.add_argument("-n", "--new_index", type=str, required=False,
                        help="指定新索引名称。如果未指定，则默认使用格式: '{别名}_$(当前时间戳)'，例如: rc_android_attack_1719000000")

    parser.add_argument("-a", "--alias", type=str, required=False,
                        help="目标索引别名。用于查询当前索引、设置新索引别名及后续索引切换。")

    parser.add_argument("-m", "--mapping_file_path", type=str, required=False,
                        help="指定自定义映射文件路径。如果未指定，则尝试使用命令行参数 -M 中的映射内容，\n"
                             "若两者均未指定，则使用默认路径: /var/www/rc-api-server/install/mapping.json")

    parser.add_argument("-M", "--mapping", type=str, required=False,
                        help="直接提供JSON格式的映射内容。示例:\n"
                             "'{\"properties\": {\"name\": {\"type\": \"text\"}}}'")

    parser.add_argument("-d", "--delete_old", action="store_true",
                        help="是否在索引切换完成后删除旧索引（默认不删除）。")

    parser.add_argument("-s", "--suffix", type=str, default="_backup",
                        help="旧索引后缀（默认: '_backup'），用于重命名旧索引。")
    return parser


def main():
    parser = parse_arguments()
    args = parser.parse_args()

    # 检查是否提供了必要的参数
    alias = args.alias
    if not alias:
        logger.error(
            f'未指定的 --alias 参数, 退出！ \n具体使用方法请查看: python3 {sys.argv[0]} -h 或者 python3 {sys.argv[0]} --help'
        )
        parser.print_help()
        sys.exit(1)

    # 读取 mapping 映射配置
    mapping = None
    mapping_file = None

    if args.mapping_file_path:
        mapping_file = args.mapping_file_path
        if not os.path.exists(mapping_file):
            logger.error(f"指定的映射文件不存在: {mapping_file}")
            sys.exit(1)
        logger.info(f"使用指定映射文件: {mapping_file}")

    elif args.mapping:
        try:
            mapping = json.loads(args.mapping)
        except json.JSONDecodeError as e:
            logger.exception(f"[Error] 无法解析JSON映射: {e}")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"[Error] 解析JSON映射时发生错误: {e}")
            sys.exit(1)

    else:
        # 默认路径兜底
        mapping_file = "/var/www/rc-api-server/install/mapping.json"
        if not os.path.exists(mapping_file):
            logger.error("[错误] 未指定映射参数，也找不到默认映射文件")
            sys.exit(1)
        logger.info(f"使用默认映射文件: {mapping_file}")

    # 如果通过文件方式处理，统一读取 mapping 结构
    if mapping is None:
        mappings = read_mapping_from_file(mapping_file)
        if not mappings:
            logger.error("[错误] 映射文件内容读取失败或格式错误.")
            sys.exit(1)
        mapping = mappings.get(alias, {}).get("mappings")
        if not mapping:
            logger.error(f"[错误] 映射文件中未找到 {alias} 对应的 mappings")
            sys.exit(1)

    if not args.old_index:
        logger.info(f"未指定 --old_index 参数，最新索引正在使用Elasticsearch查询中...")
        args.old_index = get_latest_index_by_alias(es, args.alias, mapping, args.suffix)
        if not args.old_index:
            sys.exit(1)
    old_index = args.old_index
    logger.info(f"old index name: {old_index}")

    if not args.new_index:
        logger.info(f"[信息] 未指定 --new_index 参数，使用默认值: {args.alias} + $(date +%s)")
        args.new_index = f"{args.alias}_{int(time.time())}"
    new_index = args.new_index
    logger.info(f"new index name: {args.new_index}")

    # 步骤 1: 创建新索引
    if not create_new_index(new_index, mapping):
        sys.exit(1)

    # 步骤 2: 迁移数据
    # if not reindex_data(old_index, new_index):
    #     sys.exit(1)
    logger.info(f"[信息] 开始异步迁移数据...")
    success = reindex_data_async(old_index, new_index)
    if not success:
        logger.error("[错误] 异步 reindex 失败，终止重命名")
        sys.exit(1)

    # 更新 alias
    if not update_alias_to_new_index(alias, new_index):
        sys.exit(1)

    # 步骤 3: 删除旧索引（可选）
    if args.delete_old and old_index and not delete_old_index(old_index):
        sys.exit(1)

    logger.info(f"[成功] 索引迁移完成.")
    sys.exit(0)


if __name__ == "__main__":
    main()
