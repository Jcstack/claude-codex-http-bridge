/* Synthetic Seatbelt preflight. All paths and the listener are fixture-owned. */
#include <arpa/inet.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

static const char *json_bool(int value) {
    return value ? "true" : "false";
}

static int can_read(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    char byte;
    int readable = read(fd, &byte, 1) == 1;
    close(fd);
    return readable;
}

static int can_create(const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return 0;
    close(fd);
    unlink(path);
    return 1;
}

static int can_connect(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return 0;
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons((unsigned short)port);
    int connected = connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0;
    close(fd);
    return connected;
}

int main(int argc, char **argv) {
    if (argc != 7) return 2;
    char *end = NULL;
    long port = strtol(argv[6], &end, 10);
    if (!end || *end || port < 1 || port > 65535) return 2;
    printf("{\"files\":[%s,%s,%s,%s,%s],\"other_port_connected\":%s}\n",
           json_bool(can_read(argv[1])), json_bool(can_read(argv[2])),
           json_bool(can_read(argv[3])), json_bool(can_read(argv[4])),
           json_bool(can_create(argv[5])), json_bool(can_connect((int)port)));
    return 0;
}
