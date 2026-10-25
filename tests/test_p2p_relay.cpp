// SPDX-License-Identifier: GPL-2.0-or-later
// The bbnet20 relay's synthetic endpoints and relay-by-id fallback: no sockets, no server, no
// game. Build (needs the online module's libraries): ninja -C out/gpu p2p-relay-test &&
// out/gpu/p2p-relay-test.
#include <arpa/inet.h>
#include <netinet/in.h>

#include <cstdio>
#include <cstring>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "core/net/p2p_relay.h"

namespace Core::Net::P2P {
// p2p_transport.cpp (which defines the rest of Endpoint) is not part of this test.
Endpoint Endpoint::FromSockaddr(const sockaddr* sa, socklen_t len) {
    Endpoint e;
    std::memcpy(&e.addr, sa, len);
    return e;
}
} // namespace Core::Net::P2P

namespace {
using namespace Core::Net::P2P;

int failures = 0;

void Check(bool ok, const char* what, int line) {
    if (!ok) {
        std::printf("FAIL line %d: %s\n", line, what);
        ++failures;
    }
}

#define CHECK(cond) Check((cond), #cond, __LINE__)

Endpoint Make(u32 addr_nbo, u16 port_nbo) {
    sockaddr_in in{};
    in.sin_family = AF_INET;
    in.sin_addr.s_addr = addr_nbo;
    in.sin_port = port_nbo;
    return Endpoint::FromSockaddr(reinterpret_cast<const sockaddr*>(&in), sizeof(in));
}

std::string PadId(std::string_view id) {
    std::string padded(id.substr(0, 16));
    padded.resize(16, '\0');
    return padded;
}

u32 AddrOf(const Endpoint& e) {
    return reinterpret_cast<const sockaddr_in*>(&e.addr)->sin_addr.s_addr;
}

void SetEnv(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value);
#else
    setenv(name, value, 1);
#endif
}
} // namespace

int main() {
    // The relay is a singleton: these checks run in order and share its peer table.
    SetEnv("SHADPS4_P2P_RELAY", "");
    auto& relay = Relay::Instance();
    const std::string peer_a = "GoodHunter";
    const std::string peer_b = "LadyMaria";
    const u32 server = inet_addr("203.0.113.1");
    const u16 relay_port = htons(3478);
    const std::vector<u8> frame = {'f', 'r', 'a', 'm', 'e'};
    const auto nothing = [](std::span<const u8>, const Endpoint&) {};
    std::vector<u8> last;
    const auto record = [&last](std::span<const u8> p, const Endpoint&) {
        last.assign(p.begin(), p.end());
    };

    // The synthetic endpoint is deterministic, link-local and the same for a padded id.
    CHECK(SyntheticPeerAddr(peer_a) == SyntheticPeerAddr(peer_a));
    CHECK(SyntheticPeerAddr(peer_a) == SyntheticPeerAddr(PadId(peer_a)));
    CHECK(SyntheticPeerAddr(peer_a) != SyntheticPeerAddr(peer_b));
    CHECK(IsSyntheticPeerAddr(SyntheticPeerAddr(peer_a)));
    CHECK(!IsSyntheticPeerAddr(inet_addr("192.168.1.2")));
    CHECK(SyntheticPeerPort() == htons(3658));

    // Relay off (no server): everything direct, as before.
    CHECK(!relay.Enabled());
    CHECK(relay.Route(Make(SyntheticPeerAddr(peer_a), SyntheticPeerPort()), frame, nothing) ==
          Relay::Path::Direct);

    relay.SetServer(server);
    relay.SetLocalId("Me");
    CHECK(relay.Enabled());

    // A peer with no endpoint is addressable at its synthetic endpoint and relayed only.
    relay.RememberPeer(peer_a, 0, 0);
    CHECK(relay.Route(Make(SyntheticPeerAddr(peer_a), SyntheticPeerPort()), frame, record) ==
          Relay::Path::Relay);
    CHECK(last.size() == 5 + 2 * 16 + 6 + frame.size());
    CHECK(std::memcmp(last.data(), "BBR1", 4) == 0);
    CHECK(last[4] == 0x03);
    CHECK(std::memcmp(last.data() + 5, PadId(peer_a).data(), 16) == 0);
    CHECK(std::memcmp(last.data() + 21, PadId("Me").data(), 16) == 0);

    // The game addressing 0.0.0.0 still reaches the one peer.
    last.clear();
    CHECK(relay.Route(Make(0, htons(3658)), frame, record) == Relay::Path::Relay);
    CHECK(!last.empty());

    // A synthetic address with no peer behind it is never guessed at (it would reach the wrong
    // peer) and never handed to the OS.
    CHECK(relay.Route(Make(SyntheticPeerAddr("Nobody"), SyntheticPeerPort()), frame, record) ==
          Relay::Path::Dropped);

    // Broadcast and multicast are never relayed.
    CHECK(relay.Route(Make(htonl(0xFFFFFFFFu), htons(3658)), frame, record) ==
          Relay::Path::Direct);
    CHECK(relay.Route(Make(inet_addr("224.0.0.1"), htons(3658)), frame, record) ==
          Relay::Path::Direct);

    // A real endpoint keeps the old behaviour: forced relay plus a direct copy.
    relay.RememberPeer(peer_b, inet_addr("198.51.100.7"), htons(4000));
    CHECK(relay.Route(Make(inet_addr("198.51.100.7"), htons(4000)), frame, record) ==
          Relay::Path::Both);

    // Two peers: an unknown destination is not guessed.
    CHECK(relay.Route(Make(0, htons(3658)), frame, record) == Relay::Path::Direct);

    // A frame from a peer the relay knows nothing about arrives from its synthetic endpoint.
    std::vector<u8> incoming = {'B', 'B', 'R', '1', 0x04};
    const std::string new_id = PadId("NewHunter");
    const u8 ep[6] = {0, 0, 0, 0, 0, 0};
    incoming.insert(incoming.end(), reinterpret_cast<const u8*>(new_id.data()),
                    reinterpret_cast<const u8*>(new_id.data()) + new_id.size());
    incoming.insert(incoming.end(), std::begin(ep), std::end(ep));
    incoming.push_back('h');
    incoming.push_back('i');
    const auto got = relay.OnReceive(incoming, Make(server, relay_port));
    CHECK(got.unwrapped);
    CHECK(AddrOf(got.from) == SyntheticPeerAddr("NewHunter"));

    // The relay says the peer is not registered: a synthetic peer is dropped, never sent to
    // 169.254.x.y.
    std::vector<u8> rejected = {'B', 'B', 'R', '1', 0x05};
    const std::string pa = PadId(peer_a);
    rejected.insert(rejected.end(), reinterpret_cast<const u8*>(pa.data()),
                    reinterpret_cast<const u8*>(pa.data()) + pa.size());
    relay.OnReceive(rejected, Make(server, relay_port));
    CHECK(relay.Route(Make(SyntheticPeerAddr(peer_a), SyntheticPeerPort()), frame, record) ==
          Relay::Path::Dropped);

    // Relay off again: nothing is relayed, as before. A remembered synthetic peer is dropped
    // rather than sent to its 169.254 address.
    relay.SetServer(0);
    CHECK(!relay.Enabled());
    CHECK(relay.Route(Make(SyntheticPeerAddr(peer_a), SyntheticPeerPort()), frame, record) ==
          Relay::Path::Dropped);
    CHECK(relay.Route(Make(0, htons(3658)), frame, record) == Relay::Path::Direct);

    std::printf(failures == 0 ? "p2p relay: all checks passed\n"
                              : "p2p relay: %d check(s) failed\n",
                failures);
    return failures == 0 ? 0 : 1;
}
