#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"
#include "kernel/fcntl.h"

int
main(void)
{
  char buf[128];
  int fd;
  int n;

  // Ensure report.txt exists before testing.
  fd = open("report.txt", O_CREATE | O_WRONLY);
  if (fd >= 0)
    close(fd);

  // Read the designated untrusted file.
  fd = open("inbox.txt", O_RDONLY);
  if (fd < 0) {
    printf("FAIL: cannot open inbox.txt\n");
    exit(1);
  }

  n = read(fd, buf, sizeof(buf));
  close(fd);

  if (n <= 0) {
    printf("FAIL: cannot read inbox.txt\n");
    exit(1);
  }

  printf("Read inbox.txt: %d bytes\n", n);

  // This should fail if taint tracking is working.
  if (unlink("report.txt") < 0) {
    printf("PASS: unlink blocked for tainted process\n");
  } else {
    printf("FAIL: unlink succeeded for tainted process\n");
  }

  exit(0);
}
