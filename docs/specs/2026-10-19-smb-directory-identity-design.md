# NAS 目录身份查询兼容性修复

## 复现与原因

0.5.4 的多演员任务在列出归档目录之前，取得演员目录和祖先的句柄身份，以避免 UNC、映射盘或祖孙目录重复占用。部分 NAS 支持目录浏览和传统文件身份接口，却对 `GetFileInformationByHandleEx(FileIdInfo)` 返回 `ERROR_INVALID_PARAMETER (87)`。当前实现直接抛出此错误，导致双栏目录无法使用。空目录清理使用同样的128位接口，也受此限制影响。

在用户报告目录及全部祖先上的只读探针确认：FileIdInfo均返回87；FileAttributeTagInfo、GetFinalPathNameByHandleW、GetFileInformationByHandle和GetVolumeInformationByHandleW均成功；文件系统报告NTFS，传统身份非零且与原stat身份一致。真实路径和编号仅在本地诊断中保留，不写入公开材料。

## 方案与边界

共享句柄身份读取函数供归档资源占用和清理使用。优先保留128位接口；只在错误1、50、87时，使用同一个打开的句柄读取传统卷序列号及64位文件编号。fallback需确认支持其身份语义的文件系统，拒绝ReFS、无法确认的文件系统、零或无效编号。权限、断线和其他异常不触发兼容回退。

不得截断128位编号或卷号来推断64位身份。128位成功时可从同一句柄取得兼容身份作为别名桥接；资源占用比较演员和祖先的已验证身份集合，覆盖128/64混合查询和映射盘/UNC别名。不能证明独立的混合身份不应放行。清理仍要求实际删除句柄与选定身份和规范路径完全匹配，模式变化可安全拒绝。

不改变目标提交、校验、ADS检查、源删除、队列并发或UI。不为写操作增加自动重试。真实NAS只做读取；删除/传输测试均用临时目录和隔离数据库。

## 验证

先建立错误87的回归RED，然后验证三个允许回退错误、其他错误不回退、同句柄读取、零/无效编号、ReFS/未知文件系统、非目录/重解析点、句柄关闭和原128位路径。覆盖身份混合情况下同目录及祖先资源重叠、不同目录可并发、清理绑定不变。修复后对报告目录重复只读capture和列举，完整后端回归、锁文件检查及Windows CI通过后，由Kimi K3唯一外审。

补丁版本0.5.5沿用既有桌面安装包交付流程；不安装，不执行真实影片移动或清理。

## 依据与设计质询

[Microsoft GetFileInformationByHandle 文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle)规定卷序列号与文件编号用于比较两个句柄是否指向同一目录；网络后端可能返回不完整信息，须核验结果。[BY_HANDLE_FILE_INFORMATION 文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information)明确ReFS上的64位编号不保证唯一，因此不允许该回退。

内部质询指出仅给64位编号加tag不足以保护混合别名，且不能把所有错误都当接口不支持；方案据此加入同句柄身份桥接和精确错误白名单。删除仍保留句柄绑定及最终路径验证。
