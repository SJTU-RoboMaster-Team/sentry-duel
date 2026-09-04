"""upload.py 多文件源码包功能的单元测试 + 真实编译测试。"""
import io
import tarfile
import zipfile

import pytest

import upload
from conftest import (
    BROKEN_AI, HELPER_CPP, HELPER_H, MAKEFILE_FAIL, MAKEFILE_NO_SO,
    MAKEFILE_OK, MINIMAL_AI, MY_AI_USES_HELPER, make_targz, make_zip,
)


# ---------------- _safe_member_name ----------------

class TestSafeMemberName:
    def test_plain_relative_path(self):
        assert upload._safe_member_name("src/my_ai.cpp") == "src/my_ai.cpp"

    def test_dot_prefix_normalized(self):
        assert upload._safe_member_name("./my_ai.cpp") == "my_ai.cpp"

    def test_backslash_normalized(self):
        assert upload._safe_member_name("src\\my_ai.cpp") == "src/my_ai.cpp"

    def test_directory_entry_returns_empty(self):
        assert upload._safe_member_name("src/") == ""
        assert upload._safe_member_name("") == ""

    def test_absolute_path_rejected(self):
        with pytest.raises(RuntimeError):
            upload._safe_member_name("/etc/passwd")

    def test_dotdot_rejected(self):
        with pytest.raises(RuntimeError):
            upload._safe_member_name("../evil.cpp")
        with pytest.raises(RuntimeError):
            upload._safe_member_name("a/../../evil.cpp")

    def test_tilde_rejected(self):
        with pytest.raises(RuntimeError):
            upload._safe_member_name("~/evil.cpp")


# ---------------- _extract_archive ----------------

class TestExtractArchive:
    def test_zip_normal(self, tmp_path):
        data = make_zip({"my_ai.cpp": MINIMAL_AI.encode(),
                         "sub/helper.h": HELPER_H.encode()})
        dest = tmp_path / "out"
        upload._extract_archive(data, "pack.zip", dest)
        assert (dest / "my_ai.cpp").read_text() == MINIMAL_AI
        assert (dest / "sub" / "helper.h").exists()

    def test_targz_normal(self, tmp_path):
        data = make_targz({"my_ai.cpp": MINIMAL_AI.encode()})
        dest = tmp_path / "out"
        upload._extract_archive(data, "pack.tar.gz", dest)
        assert (dest / "my_ai.cpp").exists()

    def test_unsupported_extension(self, tmp_path):
        with pytest.raises(RuntimeError, match="仅支持"):
            upload._extract_archive(b"xxxx", "pack.rar", tmp_path / "out")

    def test_empty_archive_rejected(self, tmp_path):
        data = make_zip({})
        with pytest.raises(RuntimeError, match="没有文件"):
            upload._extract_archive(data, "pack.zip", tmp_path / "out")

    def test_zip_traversal_rejected(self, tmp_path):
        data = make_zip({"../evil.cpp": b"x"})
        with pytest.raises(RuntimeError, match="越界"):
            upload._extract_archive(data, "pack.zip", tmp_path / "out")
        assert not (tmp_path / "evil.cpp").exists()

    def test_zip_symlink_rejected(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("link.cpp")
            # unix 模式:符号链接
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, b"/etc/passwd")
        with pytest.raises(RuntimeError, match="符号链接"):
            upload._extract_archive(buf.getvalue(), "pack.zip", tmp_path / "out")

    def test_tar_symlink_rejected(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("real.cpp")
            content = b"x"
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
            link = tarfile.TarInfo("link.cpp")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        with pytest.raises(RuntimeError, match="链接"):
            upload._extract_archive(buf.getvalue(), "pack.tar.gz",
                                    tmp_path / "out")

    def test_too_many_files(self, tmp_path):
        data = make_zip({f"f{i}.txt": b"x" for i in range(65)})
        with pytest.raises(RuntimeError, match="文件数"):
            upload._extract_archive(data, "pack.zip", tmp_path / "out")

    def test_member_too_large(self, tmp_path):
        big = b"x" * (upload.PACK_MAX_MEMBER + 1)
        data = make_zip({"big.bin": big})
        with pytest.raises(RuntimeError, match="成员过大"):
            upload._extract_archive(data, "pack.zip", tmp_path / "out")

    def test_total_too_large(self, tmp_path):
        chunk = b"x" * (7 * 1024 * 1024)
        data = make_zip({f"f{i}.bin": chunk for i in range(3)})
        with pytest.raises(RuntimeError, match="总大小"):
            upload._extract_archive(data, "pack.zip", tmp_path / "out")

    def test_existing_dest_replaced(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        (dest / "stale.txt").write_text("junk")
        data = make_zip({"my_ai.cpp": MINIMAL_AI.encode()})
        upload._extract_archive(data, "pack.zip", dest)
        assert not (dest / "stale.txt").exists()
        assert (dest / "my_ai.cpp").exists()


# ---------------- _find_project_root ----------------

class TestFindProjectRoot:
    def test_root_level_cpp(self, tmp_path):
        (tmp_path / "my_ai.cpp").write_text("x")
        assert upload._find_project_root(tmp_path) == tmp_path

    def test_root_level_makefile(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:")
        assert upload._find_project_root(tmp_path) == tmp_path

    def test_single_wrapping_dir(self, tmp_path):
        inner = tmp_path / "my-project"
        inner.mkdir()
        (inner / "my_ai.cpp").write_text("x")
        assert upload._find_project_root(tmp_path) == inner

    def test_double_wrapping_dir(self, tmp_path):
        inner = tmp_path / "a" / "b"
        inner.mkdir(parents=True)
        (inner / "my_ai.cpp").write_text("x")
        assert upload._find_project_root(tmp_path) == inner

    def test_no_hit_returns_pkg_dir(self, tmp_path):
        (tmp_path / "readme.txt").write_text("x")
        assert upload._find_project_root(tmp_path) == tmp_path


# ---------------- compile_package(真实 g++ 编译) ----------------

class TestCompilePackage:
    def test_zip_multifile_no_makefile(self):
        data = make_zip({
            "my_ai.cpp": MY_AI_USES_HELPER.encode(),
            "helper.cpp": HELPER_CPP.encode(),
            "helper.h": HELPER_H.encode(),
        })
        cpp, so = upload.compile_package(data, "pack.zip", "t_author1", "multi")
        assert so.exists() and so.name == "multi.so"
        assert cpp.name == "my_ai.cpp"

    def test_wrapping_dir_zip(self):
        data = make_zip({
            "outer/my_ai.cpp": MY_AI_USES_HELPER.encode(),
            "outer/helper.cpp": HELPER_CPP.encode(),
            "outer/helper.h": HELPER_H.encode(),
        })
        cpp, so = upload.compile_package(data, "pack.zip", "t_author2", "wrap")
        assert so.exists()

    def test_targz_with_makefile(self):
        data = make_targz({
            "my_ai.cpp": MINIMAL_AI.encode(),
            "Makefile": MAKEFILE_OK.encode(),
        })
        cpp, so = upload.compile_package(data, "pack.tar.gz", "t_author3", "mk")
        assert so.exists() and so.name == "mk.so"

    def test_makefile_failure(self):
        data = make_zip({
            "my_ai.cpp": MINIMAL_AI.encode(),
            "Makefile": MAKEFILE_FAIL.encode(),
        })
        with pytest.raises(RuntimeError, match="make 失败"):
            upload.compile_package(data, "pack.zip", "t_author4", "mkfail")

    def test_makefile_no_so_output(self):
        data = make_zip({
            "my_ai.cpp": MINIMAL_AI.encode(),
            "Makefile": MAKEFILE_NO_SO.encode(),
        })
        with pytest.raises(RuntimeError, match="make 失败或未产出"):
            upload.compile_package(data, "pack.zip", "t_author5", "mknoso")

    def test_broken_cpp_compile_error(self):
        data = make_zip({"my_ai.cpp": BROKEN_AI.encode()})
        with pytest.raises(RuntimeError, match="编译失败"):
            upload.compile_package(data, "pack.zip", "t_author6", "broken")

    def test_no_makefile_no_cpp(self):
        data = make_zip({"readme.txt": b"hello"})
        with pytest.raises(RuntimeError, match="没有 Makefile"):
            upload.compile_package(data, "pack.zip", "t_author7", "empty")

    def test_archive_over_20mb(self):
        data = b"x" * (upload.PACK_MAX_ARCHIVE + 1)
        with pytest.raises(RuntimeError, match="超过 20MB"):
            upload.compile_package(data, "pack.zip", "t_author8", "huge")

    def test_main_cpp_fallback_first_sorted(self):
        """没有 my_ai.cpp 时,主源码取项目根排序后的第一个 .cpp。"""
        data = make_zip({
            "alpha.cpp": MINIMAL_AI.replace(
                "void act(", "void act(").encode(),
            "beta.cpp": HELPER_CPP.encode(),
            "helper.h": HELPER_H.encode(),
        })
        cpp, so = upload.compile_package(data, "pack.zip", "t_author9", "fb")
        assert so.exists()
        assert cpp.name == "alpha.cpp"
