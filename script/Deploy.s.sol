// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {QuboMarket} from "../src/QuboMarket.sol";

/// forge script script/Deploy.s.sol --rpc-url qms --broadcast --private-key $PRIVATE_KEY
contract Deploy is Script {
    function run() external returns (QuboMarket market) {
        vm.startBroadcast();
        market = new QuboMarket();
        vm.stopBroadcast();
        console2.log("QuboMarket deployed at", address(market));
    }
}
